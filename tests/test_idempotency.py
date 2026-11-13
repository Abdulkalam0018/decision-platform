"""Duplicate requests: transport idempotency keys, concurrency, leases, TTL and business duplicates."""

import threading
from datetime import timedelta

import pytest

from decision_platform.service import DecisionService, DuplicateRequest, IdempotencyKeyReuse, RequestInProgress

from .conftest import VENDOR_BASE, deep_merge


def count(svc, table="requests"):
    return svc.db.read().execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def test_same_key_same_payload_replays_without_side_effects(svc, loan):
    first = svc.submit("loan_application", loan(), "idem-1")
    second = svc.submit("loan_application", loan(), "idem-1")
    assert first.http_status == 201 and second.http_status == 200
    assert second.replayed and second.headers["Idempotent-Replayed"] == "true"
    assert first.body["request_id"] == second.body["request_id"]
    assert count(svc) == 1
    assert svc.hub.get("credit_bureau").side_effects == 1
    events = [e["event_type"] for e in svc.audit(first.body["request_id"])]
    assert events.count("idempotency.replayed") == 1


def test_key_order_in_payload_does_not_matter(svc, loan):
    p1 = loan()
    p2 = {"loan": p1["loan"], "applicant": dict(reversed(list(p1["applicant"].items())))}
    a = svc.submit("loan_application", p1, "idem-order")
    b = svc.submit("loan_application", p2, "idem-order")
    assert a.body["request_id"] == b.body["request_id"] and b.replayed


def test_same_key_different_payload_is_rejected(svc, loan):
    svc.submit("loan_application", loan(), "idem-2")
    with pytest.raises(IdempotencyKeyReuse):
        svc.submit("loan_application", loan(loan={"amount": 700000}), "idem-2")
    assert count(svc) == 1


def test_keys_are_scoped_per_workflow(svc, loan, claim):
    a = svc.submit("loan_application", loan(), "shared-key")
    b = svc.submit("insurance_claim", claim(), "shared-key")
    assert a.body["request_id"] != b.body["request_id"]


def test_in_flight_request_returns_409(svc, loan, clock, monkeypatch):
    """Simulate a client retry while the original is still processing (lease held)."""
    gate = threading.Event()
    original_run = svc.engine.run

    def slow_run(rid, actor="engine"):
        gate.wait(5)
        return original_run(rid, actor)

    monkeypatch.setattr(svc.engine, "run", slow_run)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("r", svc.submit("loan_application", loan(), "idem-3")))
    t.start()
    for _ in range(200):  # wait until the key row exists
        if count(svc, "idempotency_keys"):
            break
        threading.Event().wait(0.01)
    with pytest.raises(RequestInProgress):
        svc.submit("loan_application", loan(), "idem-3")
    gate.set()
    t.join()
    assert result["r"].body["status"] == "APPROVED"


def test_concurrent_duplicates_create_exactly_one_request(db, clock, loan):
    from .conftest import WORKFLOWS

    svc = DecisionService(db, clock=clock, sleep=lambda s: None)
    svc.load_workflows(WORKFLOWS)
    results, errors = [], []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        try:
            results.append(svc.submit("loan_application", loan(), "race-key"))
        except RequestInProgress as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert count(svc) == 1
    assert len({r.body["request_id"] for r in results}) == 1
    assert len(results) + len(errors) == 8
    assert svc.hub.get("credit_bureau").side_effects == 1


def test_lease_expiry_allows_takeover_after_crash(svc, loan, clock, monkeypatch):
    def crash(rid, actor="engine"):
        raise RuntimeError("worker died")

    monkeypatch.setattr(svc.engine, "run", crash)
    with pytest.raises(RuntimeError):
        svc.submit("loan_application", loan(), "idem-crash")
    monkeypatch.undo()

    with pytest.raises(RequestInProgress):  # lease still held
        svc.submit("loan_application", loan(), "idem-crash")
    clock.advance(31)
    res = svc.submit("loan_application", loan(), "idem-crash")
    assert res.body["status"] == "APPROVED" and count(svc) == 1
    assert "idempotency.lease_takeover" in [e["event_type"] for e in svc.audit(res.body["request_id"])]


def test_idempotency_key_expires_after_ttl(svc, claim, clock):
    first = svc.submit("insurance_claim", claim(claim_id="CLM-TTL-1"), "ttl-key")
    clock.advance(timedelta(hours=25).total_seconds())
    second = svc.submit("insurance_claim", claim(claim_id="CLM-TTL-2"), "ttl-key")
    assert second.http_status == 201 and second.body["request_id"] != first.body["request_id"]


def test_business_duplicate_returns_existing_decision(svc, loan):
    first = svc.submit("loan_application", loan(), "client-retry-a")
    dup = svc.submit("loan_application", loan(), "client-retry-b")  # new key, same applicant/purpose/amount
    assert dup.duplicate_of == first.body["request_id"] and dup.body["request_id"] == first.body["request_id"]
    assert count(svc) == 1
    again = svc.submit("loan_application", loan(), "client-retry-b")  # the new key is now bound too
    assert again.body["request_id"] == first.body["request_id"]


def test_business_duplicate_outside_window_is_new(svc, loan, clock):
    first = svc.submit("loan_application", loan(), "w-1")
    clock.advance(timedelta(hours=25).total_seconds())
    later = svc.submit("loan_application", loan(), "w-2")
    assert later.http_status == 201 and later.body["request_id"] != first.body["request_id"]


def test_business_duplicate_reject_policy(svc):
    svc.submit("vendor_approval", VENDOR_BASE, "vendor-a")
    renamed = deep_merge(VENDOR_BASE, {"vendor": {"name": "Acme Facility Svcs Pvt Ltd"}})
    with pytest.raises(DuplicateRequest) as exc:
        svc.submit("vendor_approval", renamed, "vendor-b")
    assert exc.value.details["existing_request_id"]
