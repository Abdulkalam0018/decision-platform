"""Partial save failures: a crash mid-transaction leaves no half-written state, and recovery is exact."""

import pytest

from decision_platform.persistence.db import InjectedFault


def snapshot(svc):
    conn = svc.db.read()
    return {
        t: conn.execute(f"SELECT COUNT(*) c FROM {t}").fetchone()["c"]
        for t in ("requests", "stage_runs", "transitions", "audit_events", "outbox", "idempotency_keys")
    }


def fail_at(svc, point, after_n=1):
    hits = {"n": 0}

    def injector(name):
        if name == point:
            hits["n"] += 1
            if hits["n"] == after_n:
                raise InjectedFault(name)

    svc.db.fault_injector = injector


@pytest.mark.parametrize("point", ["after_stage_run", "after_stage_audit", "after_request_update", "before_commit"])
def test_failure_inside_step_rolls_back_entire_step(svc, loan, point):
    # 3rd occurrence = the credit_check step (eligibility, affordability, credit_check)
    after_n = 3 if point != "before_commit" else 6  # before_commit also fires for intake + start + call intent
    fail_at(svc, point, after_n)
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), f"ps-{point}")
    svc.db.fault_injector = None

    rid = svc.list_requests()[0]["request_id"]
    hist = svc.history(rid)
    done = [r["stage_id"] for r in hist["stage_runs"]]
    # Either the whole step committed or none of it did: stage_runs and current_stage always agree.
    assert hist["request"]["current_stage"] == ("credit_check" if "credit_check" not in done else "fraud_check")
    assert svc.verify_audit()["ok"]
    stage_events = [e["payload"]["stage"] for e in svc.audit(rid) if e["event_type"] == "stage.completed"]
    assert stage_events == done


def test_recovery_after_partial_save_completes_exactly_once(svc, loan, clock):
    fail_at(svc, "after_stage_run", 3)
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "ps-recover")
    svc.db.fault_injector = None
    assert svc.hub.get("credit_bureau").side_effects == 1

    clock.advance(31)  # idempotency lease expired: client retry takes over
    res = svc.submit("loan_application", loan(), "ps-recover")
    assert res.body["status"] == "APPROVED"
    runs = [r["stage_id"] for r in svc.history(res.body["request_id"])["stage_runs"]]
    assert runs == ["eligibility", "affordability", "credit_check", "fraud_check", "risk_assessment", "routing"]
    assert svc.hub.get("credit_bureau").side_effects == 1  # replayed via idempotency token, not re-executed
    assert snapshot(svc)["outbox"] == 1


def test_failure_during_intake_persists_nothing(svc, loan):
    fail_at(svc, "before_commit", 1)
    before = snapshot(svc)
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "ps-intake")
    svc.db.fault_injector = None
    assert snapshot(svc) == before
    assert svc.submit("loan_application", loan(), "ps-intake").http_status == 201


def test_failure_on_terminal_step_does_not_emit_decision(svc, loan):
    fail_at(svc, "after_request_update", 6)  # routing -> approve
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "ps-final")
    svc.db.fault_injector = None
    s = snapshot(svc)
    assert s["outbox"] == 0
    assert svc.list_requests()[0]["status"] == "IN_PROGRESS"


def test_review_failure_rolls_back(svc, loan):
    rid = svc.submit("loan_application", loan(applicant={"employment_status": "unemployed"}), "ps-rv").body[
        "request_id"
    ]
    fail_at(svc, "before_commit", 1)
    with pytest.raises(InjectedFault):
        svc.review(rid, "approve", "u1", "income verified")
    svc.db.fault_injector = None
    assert svc.get(rid)["status"] == "MANUAL_REVIEW"
    assert svc.db.read().execute("SELECT COUNT(*) c FROM reviews").fetchone()["c"] == 0
