"""Retry flow: parked requests resume from their checkpoint, worker scheduling, budget exhaustion."""

import pytest
import yaml

from decision_platform.service import InvalidState

from .conftest import WORKFLOWS, statuses


def park(svc, loan, key="rt-1", dep="credit_bureau", **override):
    svc.hub.get(dep).configure(fail_next=["transient"] * 3)
    res = svc.submit("loan_application", loan(**override), key).body
    assert res["status"] == "RETRY_PENDING"
    return res


def test_manual_retry_resumes_and_completes(svc, loan):
    res = park(svc, loan)
    final = svc.retry(res["request_id"])
    assert final["status"] == "APPROVED"
    assert statuses(svc, res["request_id"]) == ["RECEIVED", "IN_PROGRESS", "RETRY_PENDING", "IN_PROGRESS", "APPROVED"]


def test_resume_does_not_rerun_completed_stages(svc, loan):
    res = park(svc, loan, dep="fraud_screen")  # credit succeeded, fraud failed
    credit = svc.hub.get("credit_bureau")
    assert credit.side_effects == 1
    svc.retry(res["request_id"])
    assert credit.side_effects == 1 and credit.calls == 1  # credit stage never executed again
    stage_ids = [r["stage_id"] for r in svc.history(res["request_id"])["stage_runs"]]
    assert stage_ids.count("credit_check") == 1 and stage_ids.count("fraud_check") == 2


def test_worker_only_processes_due_retries(svc, loan, clock):
    res = park(svc, loan)
    assert svc.process_due_retries()["processed"] == 0  # not due yet
    clock.advance(16 * 60)  # beyond max delay
    out = svc.process_due_retries()
    assert out["results"] == [{"request_id": res["request_id"], "from": "RETRY_PENDING", "to": "APPROVED"}]


def test_retry_budget_exhaustion_goes_to_manual_review(svc, loan, clock):
    svc.hub.get("credit_bureau").configure(down=True)
    rid = svc.submit("loan_application", loan(), "rt-ex").body["request_id"]
    for _ in range(3):
        clock.advance(3600)
        svc.process_due_retries()
    final = svc.get(rid)
    assert final["status"] == "MANUAL_REVIEW" and final["retry_count"] == 3
    assert "retry budget exhausted after 3 retries" in final["decision"]["reasons"]
    assert "retry.exhausted" in [e["event_type"] for e in svc.audit(rid)]


def test_retry_budget_exhaustion_can_fail_instead(svc, loan, clock):
    raw = yaml.safe_load((WORKFLOWS / "loan_application.v1.yaml").read_text())
    raw["version"] = 30
    raw["retry"] = {"max_request_retries": 1, "base_delay_s": 1, "max_delay_s": 2, "on_exhausted": "failed"}
    svc.registry.register(raw, actor="test", activate=True)
    svc.hub.get("credit_bureau").configure(down=True)
    rid = svc.submit("loan_application", loan(), "rt-fail").body["request_id"]
    clock.advance(10)
    svc.process_due_retries()
    assert svc.get(rid)["status"] == "FAILED"


def test_retry_backoff_grows(svc, loan, clock):
    from decision_platform.domain.clock import parse_iso

    svc.hub.get("credit_bureau").configure(down=True)
    rid = svc.submit("loan_application", loan(), "rt-grow").body["request_id"]
    delays = []
    for _ in range(2):
        r = svc.get(rid)
        delays.append((parse_iso(r["next_retry_at"]) - clock.now()).total_seconds())
        clock.advance(3600)
        svc.process_due_retries()
    # retry 1 in [30, 60] (ceiling 60), retry 2 in [60, 120] (ceiling 120)
    assert 30 <= delays[0] <= 60 and 60 <= delays[1] <= 120


def test_retry_rejected_for_non_pending_request(svc, loan):
    rid = svc.submit("loan_application", loan(), "rt-np").body["request_id"]
    with pytest.raises(InvalidState):
        svc.retry(rid)


def test_worker_recovers_stale_in_progress_request(svc, loan, clock, monkeypatch):
    """Process crashed mid-workflow: the request is stuck IN_PROGRESS. The worker resumes it."""
    from decision_platform.persistence.db import InjectedFault

    def boom(point):
        if point == "after_stage_run":
            raise InjectedFault(point)

    original = svc.engine._commit_step
    calls = {"n": 0}

    def crash_on_third(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            svc.db.fault_injector = boom
        try:
            return original(*a, **kw)
        finally:
            svc.db.fault_injector = None

    monkeypatch.setattr(svc.engine, "_commit_step", crash_on_third)
    with pytest.raises(InjectedFault):
        svc.submit("loan_application", loan(), "stale-1")
    monkeypatch.undo()

    row = svc.list_requests()[0]
    assert row["status"] == "IN_PROGRESS" and row["current_stage"] == "credit_check"
    assert svc.process_due_retries()["processed"] == 0  # not stale yet
    clock.advance(6 * 60)
    out = svc.process_due_retries()
    assert out["results"][0]["to"] == "APPROVED"
    assert svc.hub.get("credit_bureau").side_effects == 1  # same idempotency token: no second credit pull
