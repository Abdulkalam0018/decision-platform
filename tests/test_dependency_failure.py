"""External dependency failures: inline retries with jitter, permanent errors, timeouts, circuit breaker."""

import yaml

from .conftest import WORKFLOWS


def stage_trace(svc, rid, stage_id):
    return next(r for r in svc.history(rid)["stage_runs"] if r["stage_id"] == stage_id)["trace"]


def test_transient_failure_recovered_by_inline_retry(svc, loan, sleeps):
    svc.hub.get("credit_bureau").configure(fail_next=["transient", "timeout"])
    res = svc.submit("loan_application", loan(), "dep-1").body
    assert res["status"] == "APPROVED"
    attempts = stage_trace(svc, res["request_id"], "credit_check")["attempts"]
    assert [a["ok"] for a in attempts] == [False, False, True]
    assert attempts[0]["error_type"] == "TransientDependencyError"
    assert attempts[1]["error_type"] == "DependencyTimeoutError"
    # full jitter: delays within [0, min(cap, base*2^n)] with base 50ms, cap 400ms
    assert len(sleeps.calls) == 2
    assert 0 <= sleeps.calls[0] <= 0.05 and 0 <= sleeps.calls[1] <= 0.1


def test_latency_above_timeout_is_a_transient_timeout(svc, loan):
    svc.hub.get("fraud_screen").configure(latency_ms=5000)
    res = svc.submit("loan_application", loan(), "dep-2").body
    assert res["status"] == "RETRY_PENDING"
    trace = stage_trace(svc, res["request_id"], "fraud_check")
    assert all(a["error_type"] == "DependencyTimeoutError" for a in trace["attempts"])


def test_exhausted_inline_retries_park_request(svc, loan, clock):
    svc.hub.get("credit_bureau").configure(fail_next=["transient"] * 3)
    res = svc.submit("loan_application", loan(), "dep-3").body
    assert res["status"] == "RETRY_PENDING"
    assert res["current_stage"] == "credit_check"
    assert res["retry_count"] == 1
    assert res["next_retry_at"] > clock.iso()
    events = [e["event_type"] for e in svc.audit(res["request_id"])]
    assert "retry.scheduled" in events and "dependency.call_started" in events


def test_permanent_error_is_not_retried_and_routes_per_config(svc, loan, claim):
    res = svc.submit("loan_application", loan(applicant={"id": "UNKNOWN-1"}), "dep-4").body
    assert res["status"] == "MANUAL_REVIEW"  # loan: on_error -> manual_review
    trace = stage_trace(svc, res["request_id"], "credit_check")
    assert trace["error_class"] == "permanent" and len(trace["attempts"]) == 1

    c = svc.submit("insurance_claim", claim(policy_number="POL-999"), "dep-5").body
    assert c["status"] == "REJECTED"  # claims: on_error -> reject (policy does not exist)


def test_injected_permanent_fault(svc, loan):
    svc.hub.get("fraud_screen").configure(fail_next=["permanent"])
    res = svc.submit("loan_application", loan(), "dep-6").body
    assert res["status"] == "MANUAL_REVIEW"
    assert "non-retryable" in res["decision"]["reasons"][0]


def test_circuit_breaker_opens_and_fails_fast(svc, loan):
    adapter = svc.hub.get("credit_bureau")
    adapter.configure(down=True)
    for i in range(2):  # 3 attempts each -> 6 failures >= threshold 5
        svc.submit("loan_application", loan(applicant={"id": f"APP-CB{i}"}), f"cb-{i}")
    assert svc.hub.breakers["loan_application:credit_bureau"].state.value == "open"
    calls_before = adapter.calls
    res = svc.submit("loan_application", loan(applicant={"id": "APP-CB9"}), "cb-9").body
    assert res["status"] == "RETRY_PENDING"
    assert adapter.calls == calls_before  # failed fast: dependency was not called at all
    trace = stage_trace(svc, res["request_id"], "credit_check")
    assert trace["error_class"] == "circuit_open" and len(trace["attempts"]) == 1


def test_circuit_breaker_half_open_recovery(svc, loan):
    t = [0.0]
    breaker = svc.hub.breaker("loan_application:credit_bureau", 5, 30, type_name="credit_bureau")
    breaker._clock = lambda: t[0]
    svc.hub.get("credit_bureau").configure(down=True)
    for i in range(2):
        svc.submit("loan_application", loan(applicant={"id": f"APP-H{i}"}), f"h-{i}")
    assert breaker.state.value == "open"
    svc.hub.get("credit_bureau").configure(down=False)
    t[0] = 31.0
    assert breaker.state.value == "half_open"
    res = svc.submit("loan_application", loan(applicant={"id": "APP-H9"}), "h-9").body
    assert res["status"] in ("APPROVED", "REJECTED", "MANUAL_REVIEW")
    assert breaker.state.value == "closed"


def test_permanent_errors_do_not_trip_breaker(svc, loan):
    for i in range(6):
        svc.submit("loan_application", loan(applicant={"id": f"UNKNOWN-{i}"}), f"p-{i}")
    assert svc.hub.breakers["loan_application:credit_bureau"].state.value == "closed"


def test_on_unavailable_manual_review_option(svc, loan):
    raw = yaml.safe_load((WORKFLOWS / "loan_application.v1.yaml").read_text())
    raw["version"] = 20
    next(s for s in raw["stages"] if s["id"] == "fraud_check")["on_unavailable"] = "manual_review"
    svc.registry.register(raw, actor="test", activate=True)
    svc.hub.get("fraud_screen").configure(down=True)
    res = svc.submit("loan_application", loan(), "ua-1").body
    assert res["status"] == "MANUAL_REVIEW"
    assert "unavailable after retries" in res["decision"]["reasons"][0]
