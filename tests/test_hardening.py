"""Regression tests for issues found by an independent review: error containment, worker isolation,
dry-run isolation, sandbox resource limits, compiler gaps, tamper detection beyond the audit table,
per-workflow breaker config, intake data minimisation, HTTP edge cases, lease heartbeat and claims."""

import contextlib
import copy
import json
import time

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decision_platform.config.compiler import WorkflowValidationError, compile_workflow
from decision_platform.config.registry import parse_definition
from decision_platform.dependencies.simulated import DependencyHub
from decision_platform.persistence import repository as repo
from decision_platform.persistence.db import InjectedFault
from decision_platform.rules.expression import EvalContext, ExpressionError, compile_expression
from decision_platform.service import RequestInProgress

from .conftest import WORKFLOWS

TYPES = DependencyHub().types


def loan_raw(version=50, name="loan_application"):
    raw = yaml.safe_load((WORKFLOWS / "loan_application.v1.yaml").read_text())
    raw["version"], raw["workflow"] = version, name
    return raw


def stage(raw, sid):
    return next(s for s in raw["stages"] if s["id"] == sid)


def compile_errors(raw):
    with pytest.raises(WorkflowValidationError) as exc:
        compile_workflow(raw, TYPES)
    return "\n".join(exc.value.errors)


# 1. error containment
def test_branch_type_error_is_contained_and_explained(svc, loan):
    raw = loan_raw()
    stage(raw, "routing")["branches"][0]["when"] = "input.applicant.name > 5"
    svc.registry.register(raw, actor="t", activate=True)
    res = svc.submit("loan_application", loan(), "h-branch").body
    assert res["status"] == "MANUAL_REVIEW"
    assert "could not be evaluated: TypeError" in res["decision"]["reasons"][0]
    assert svc.verify_audit()["ok"]


def test_precondition_type_error_is_traced_as_error(svc, loan):
    raw = loan_raw()
    stage(raw, "eligibility")["rules"][3]["when"] = "input.applicant.age > 'x'"
    svc.registry.register(raw, actor="t", activate=True)
    rid = svc.submit("loan_application", loan(), "h-when").body["request_id"]
    rule = next(r for r in svc.explain(rid)["rules_triggered"] if r["rule_id"] == "ELIG-004")
    assert rule["outcome"] == "ERROR" and "TypeError" in rule["detail"]


def test_dependency_request_expression_error_routes_on_error(svc, loan):
    raw = loan_raw()
    stage(raw, "credit_check")["request"]["applicant_id"] = "input.applicant.id + 5"
    svc.registry.register(raw, actor="t", activate=True)
    res = svc.submit("loan_application", loan(), "h-dep").body
    assert res["status"] == "MANUAL_REVIEW" and "Cannot build credit_bureau request" in res["decision"]["reasons"][0]


def test_unexpected_adapter_bug_becomes_manual_review(svc, loan, monkeypatch):
    def broken(payload, token, timeout_ms):
        raise KeyError("bug in adapter")

    monkeypatch.setattr(svc.hub.get("fraud_screen"), "invoke", broken)
    res = svc.submit("loan_application", loan(), "h-bug").body
    assert res["status"] == "MANUAL_REVIEW"
    assert "failed unexpectedly (KeyError)" in res["decision"]["reasons"][0]


def test_worker_continues_past_a_poison_request(svc, loan, clock, monkeypatch):
    svc.hub.get("credit_bureau").configure(fail_next=["transient"] * 6)
    a = svc.submit("loan_application", loan(applicant={"id": "APP-1003"}), "poison-a").body["request_id"]
    b = svc.submit("loan_application", loan(applicant={"id": "APP-1001"}), "poison-b").body["request_id"]
    clock.advance(3600)
    svc.hub.breakers["loan_application:credit_bureau"].reset()  # outage over
    real_run = svc.engine.run

    def run(rid, actor="engine"):
        if rid == a:
            raise RuntimeError("corrupt row")
        return real_run(rid, actor)

    monkeypatch.setattr(svc.engine, "run", run)
    out = svc.process_due_retries()
    by_id = {r["request_id"]: r for r in out["results"]}
    assert by_id[a]["to"] == "error" and by_id[b]["to"] == "APPROVED"
    assert "worker.error" in [e["event_type"] for e in svc.audit(a)]


# 2. dry-run isolation
def test_dry_run_does_not_touch_production_dependencies(svc, loan):
    svc.hub.get("credit_bureau").configure(down=True)
    for _ in range(3):
        assert svc.simulate("loan_application", loan())["outcome"] == "APPROVED"  # sandbox adapters are healthy
    prod = svc.hub.get("credit_bureau")
    assert prod.calls == 0 and prod.side_effects == 0 and not svc.hub.breakers


def test_bad_evaluation_date_is_input_error(svc, loan):
    from decision_platform.service import InputValidationError

    with pytest.raises(InputValidationError):
        svc.simulate("loan_application", loan(), evaluation_date="13/11/2026")


# 3. sandbox resource limits
def ev(src, variables=None):
    return compile_expression(src).evaluate(
        EvalContext(variables or {"input": {}}, __import__("datetime").date(2026, 11, 13))
    )


@pytest.mark.parametrize(
    "src",
    [
        "len(sum([[1] * 1000] * 3000))",  # quadratic list concatenation
        "'%0100000000d' % 1",  # printf-style allocation
        "len('x' * 9000 + 'y' * 9000)",  # oversized concatenation
    ],
)
def test_runtime_resource_guards(src):
    started = time.perf_counter()
    with pytest.raises(ExpressionError):
        ev(src)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    "src,msg",
    [
        ("matches(input.v, '(a+)+$')", "nested quantifiers"),
        ("matches(input.v, '[unclosed')", "invalid regex"),
        ("matches(input.v, input.pattern)", "string literal or a params"),
        ("len(input.a, 1)", "takes 1-1 arguments"),
        ("input.age is 5", "only compare with None"),
    ],
)
def test_compile_time_rejections(src, msg):
    with pytest.raises(ExpressionError, match=msg):
        compile_expression(src)


def test_step_budget_bounds_evaluation(monkeypatch):
    import decision_platform.rules.expression as expr

    monkeypatch.setattr(expr, "MAX_EVAL_STEPS", 10)
    with pytest.raises(ExpressionError, match="step budget"):
        ev("[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]")


_ATOMS = st.sampled_from(
    [
        "input.a",
        "input.b",
        "input.s",
        "input.l",
        "params.k",
        "1",
        "0",
        "-3",
        "2.5",
        "'x'",
        "''",
        "[1, 2]",
        "None",
        "true",
        "input.missing",
        "input.l[0]",
        "input.d.k",
    ]
)
_BIN = st.sampled_from(["+", "-", "*", "/", "//", "%", "==", "!=", "<", ">=", "and", "or", " in ", " not in "])
_FN = st.sampled_from(["len", "abs", "exists", "lower", "sum", "max", "round", "days_since"])


@st.composite
def expressions(draw, depth=0):
    kind = draw(st.integers(0, 3 if depth < 4 else 0))
    if kind == 0:
        return draw(_ATOMS)
    if kind == 1:
        return f"({draw(expressions(depth + 1))} {draw(_BIN)} {draw(expressions(depth + 1))})"
    if kind == 2:
        return f"{draw(_FN)}({draw(expressions(depth + 1))})"
    return f"(not {draw(expressions(depth + 1))})"


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(expressions())
def test_fuzz_interpreter_is_total_and_bounded(src):
    """Any expression either fails to compile with ExpressionError, or evaluates quickly to a value or an
    ordinary Exception that the rule engine contains. Nothing escapes as BaseException, nothing hangs."""
    try:
        ce = compile_expression(src)
    except ExpressionError:
        return
    ctx = EvalContext(
        {"input": {"a": 7, "b": 0, "s": "abc", "l": [1, 2, 3], "d": {"k": 1}}, "params": {"k": 10}},
        __import__("datetime").date(2026, 11, 13),
    )
    started = time.perf_counter()
    with contextlib.suppress(Exception):  # the rule engine contains any Exception
        ce.evaluate(ctx)
    assert time.perf_counter() - started < 0.5


# 4. tamper detection beyond audit rows
def test_forged_decision_in_requests_table_is_detected(svc, loan):
    rid = svc.submit("loan_application", loan(applicant={"id": "APP-1002"}), "tamper").body["request_id"]
    assert svc.verify_audit()["ok"]
    conn = svc.db.read()
    conn.execute(
        "UPDATE requests SET status='APPROVED', decision=replace(decision,'REJECTED','APPROVED') WHERE id=?", (rid,)
    )
    out = svc.verify_audit()
    assert not out["ok"] and out["state_mismatches"][0]["request_id"] == rid


def test_forged_decision_payload_only_is_detected(svc, loan):
    rid = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "tamper2").body["request_id"]
    svc.db.read().execute("UPDATE requests SET decision=replace(decision,'non-blocking','') WHERE id=?", (rid,))
    assert svc.verify_audit()["state_mismatches"][0]["problem"] == "decision differs from last audited decision"


def test_reviewed_decision_verifies(svc, loan):
    rid = svc.submit("loan_application", loan(applicant={"employment_status": "unemployed"}), "rv-v").body["request_id"]
    svc.review(rid, "approve", "kim", "verified pension")
    assert svc.verify_audit()["ok"]


# 5. compiler gaps
def test_remote_schema_ref_rejected():
    raw = loan_raw()
    raw["input_schema"]["properties"]["applicant"] = {"$ref": "https://evil.example/schema.json"}
    assert "non-local reference" in compile_errors(raw)


def test_regex_parameter_validated():
    raw = yaml.safe_load((WORKFLOWS / "vendor_approval.v1.yaml").read_text())
    raw["parameters"]["tax_id_pattern"] = "(a+)+$"
    assert "parameter 'tax_id_pattern' used as regex" in compile_errors(raw)


def test_reason_placeholder_typos_rejected():
    raw = loan_raw()
    stage(raw, "risk_assessment")["rules"][0]["reason"] = "Score {credit.score} below {params.min_credt_score}"
    stage(raw, "risk_assessment")["rules"][1]["reason"] = "Defaults {credt.open_defaults}"
    errs = compile_errors(raw)
    assert "unknown parameter in placeholder '{params.min_credt_score}'" in errs
    assert "unknown placeholder root 'credt'" in errs


def test_reason_placeholder_data_flow_checked():
    raw = loan_raw()
    stage(raw, "eligibility")["rules"][0]["reason"] = "Score {credit.score}"
    assert "reads 'credit.score'" in compile_errors(raw)


def test_duplicate_check_field_must_exist_in_schema():
    raw = loan_raw()
    raw["duplicate_check"]["fields"] = ["applicant.idd"]
    assert "duplicate_check field 'applicant.idd'" in compile_errors(raw)


def test_malformed_yaml_is_a_validation_error():
    with pytest.raises(WorkflowValidationError, match="not valid YAML"):
        parse_definition("workflow: [unclosed")


# 6. per-workflow breaker configuration
def test_breaker_thresholds_honoured_per_workflow(svc, loan):
    raw = loan_raw(version=1, name="loan_b")
    raw["dependencies"]["credit_bureau"]["circuit_breaker"] = {"failure_threshold": 1000, "reset_timeout_s": 30}
    raw.pop("duplicate_check")
    svc.registry.register(raw, actor="t", activate=True)
    svc.hub.get("credit_bureau").configure(down=True)
    for i in range(3):
        svc.submit("loan_application", loan(applicant={"id": f"APP-BA{i}"}), f"ba-{i}")
        svc.submit("loan_b", loan(applicant={"id": f"APP-BB{i}"}), f"bb-{i}")
    assert svc.hub.breakers["loan_application:credit_bureau"].state.value == "open"
    assert svc.hub.breakers["loan_b:credit_bureau"].state.value == "closed"
    assert svc.hub.breakers["loan_b:credit_bureau"].failure_threshold == 1000


# 7. data minimisation on rejected intake
def test_rejected_intake_audit_contains_no_submitted_values(svc, loan):
    from decision_platform.service import InputValidationError

    with pytest.raises(InputValidationError) as exc:
        svc.submit("loan_application", loan(applicant={"age": "thirty", "email": "secret.person@corp"}), "pii")
    assert any("thirty" in e["message"] for e in exc.value.details["errors"])  # client still gets detail
    stored = svc.db.read().execute("SELECT payload FROM audit_events WHERE event_type='intake.rejected'").fetchone()
    assert "thirty" not in stored["payload"] and "secret.person" not in stored["payload"]
    assert {e["field"] for e in json.loads(stored["payload"])["errors"]} == {"applicant.age", "applicant.email"}


# 8. HTTP / idempotency edge cases
def test_numerically_equal_payloads_share_a_fingerprint(svc, loan):
    a = svc.submit("loan_application", loan(loan={"amount": 600000}), "num")
    b = svc.submit("loan_application", loan(loan={"amount": 600000.0}), "num")
    assert b.replayed and a.body["request_id"] == b.body["request_id"]


def test_review_retry_is_idempotent(svc, loan):
    from decision_platform.service import InvalidState

    rid = svc.submit("loan_application", loan(applicant={"employment_status": "unemployed"}), "rv-i").body["request_id"]
    first = svc.review(rid, "approve", "kim", "pension verified")
    again = svc.review(rid, "approve", "kim", "pension verified")
    assert first == again
    assert svc.db.read().execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 1
    with pytest.raises(InvalidState):
        svc.review(rid, "reject", "lee", "disagree")


def test_http_malformed_yaml_and_bad_date(svc, loan):
    from fastapi.testclient import TestClient

    from decision_platform.api.app import create_app

    client = TestClient(create_app(svc), raise_server_exceptions=False)
    assert client.post("/v1/workflows", content="workflow: [unclosed").status_code == 422
    assert client.post("/v1/workflows/validate", content=": : :").status_code == 422
    r = client.post("/v1/workflows/loan_application/simulate", json={"input": loan(), "evaluation_date": "nope"})
    assert r.status_code == 422


# 9. lease heartbeat and resume claims
def test_lease_is_renewed_while_request_makes_progress(svc, loan, clock):
    svc.engine.sleep = lambda s: clock.advance(20)  # each inline retry "takes" 20 s
    svc.hub.get("credit_bureau").configure(fail_next=["transient", "transient"])
    hits = {"n": 0}

    def crash_on_fraud(point):
        if point == "after_stage_run":
            hits["n"] += 1
            if hits["n"] == 4:  # fraud_check step
                raise InjectedFault(point)

    svc.db.fault_injector = crash_on_fraud
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "lease")
    svc.db.fault_injector = None
    # Started at t0, lease t0+30. Credit stage committed at t0+40 and renewed the lease to t0+70.
    clock.advance(5)  # t0+45: past the original lease, inside the renewed one
    with pytest.raises(RequestInProgress):
        svc.submit("loan_application", loan(), "lease")
    clock.advance(30)
    assert svc.submit("loan_application", loan(), "lease").body["status"] == "APPROVED"


def test_two_resumers_cannot_both_claim(svc, loan, monkeypatch):
    hits = {"n": 0}

    def crash(point):
        if point == "after_stage_run":
            hits["n"] += 1
            if hits["n"] == 3:
                raise InjectedFault(point)

    svc.db.fault_injector = crash
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "claim")
    svc.db.fault_injector = None
    rid = svc.list_requests()[0]["request_id"]
    stale = repo.get_request(svc.db.read(), rid)
    svc.engine._claim(copy.deepcopy(stale), "worker-1")
    with pytest.raises(repo.ConcurrencyConflict):
        svc.engine._claim(copy.deepcopy(stale), "worker-2")


# 10. FAILED requests are not business duplicates
def test_failed_request_can_be_resubmitted(svc, loan):
    raw = loan_raw(version=60)
    raw["retry"] = {"max_request_retries": 0, "base_delay_s": 1, "max_delay_s": 1, "on_exhausted": "failed"}
    svc.registry.register(raw, actor="t", activate=True)
    svc.hub.get("credit_bureau").configure(down=True)
    first = svc.submit("loan_application", loan(), "f-1").body
    assert first["status"] == "FAILED"
    svc.hub.get("credit_bureau").configure(down=False)
    svc.hub.breakers["loan_application:credit_bureau"].reset()
    second = svc.submit("loan_application", loan(), "f-2")
    assert second.http_status == 201 and second.body["status"] == "APPROVED"
