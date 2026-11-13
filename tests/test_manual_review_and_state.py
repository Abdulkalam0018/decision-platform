"""Manual review stage and the request state machine."""

import pytest

from decision_platform.domain.models import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    RequestStatus,
    check_transition,
)
from decision_platform.service import InputValidationError, InvalidState

from .conftest import statuses


@pytest.fixture
def review_request(svc, loan):
    return svc.submit("loan_application", loan(applicant={"employment_status": "unemployed"}), "mr-1").body


def test_reviewer_approves(svc, review_request):
    rid = review_request["request_id"]
    out = svc.review(rid, "approve", "underwriter.kim", "Pension income verified via bank statements")
    assert out["status"] == "APPROVED"
    d = out["decision"]
    assert d["decided_by"] == "reviewer:underwriter.kim" and d["engine_recommendation"] == "MANUAL_REVIEW"
    assert d["final"] is True
    assert statuses(svc, rid)[-2:] == ["MANUAL_REVIEW", "APPROVED"]
    assert "Manual review by underwriter.kim: approve" in svc.explain(rid)["narrative"]


def test_reviewer_rejects_and_outbox_notified(svc, review_request):
    rid = review_request["request_id"]
    svc.review(rid, "reject", "underwriter.kim", "No verifiable income")
    events = [r["event_type"] for r in svc.db.read().execute("SELECT event_type FROM outbox ORDER BY id")]
    assert events == ["review.requested", "decision.made"]


def test_second_review_is_refused(svc, review_request):
    rid = review_request["request_id"]
    svc.review(rid, "approve", "a", "looks fine")
    with pytest.raises(InvalidState):
        svc.review(rid, "reject", "b", "changed my mind")


def test_review_requires_justification(svc, review_request):
    with pytest.raises(InputValidationError):
        svc.review(review_request["request_id"], "approve", "a", "")
    with pytest.raises(InputValidationError):
        svc.review(review_request["request_id"], "maybe", "a", "hmm ok")


def test_review_only_in_manual_review_state(svc, loan):
    rid = svc.submit("loan_application", loan(), "mr-2").body["request_id"]
    with pytest.raises(InvalidState):
        svc.review(rid, "reject", "a", "override attempt")


def test_terminal_states_have_no_exits():
    for s in (RequestStatus.APPROVED, RequestStatus.REJECTED, RequestStatus.FAILED):
        assert s.is_terminal and ALLOWED_TRANSITIONS[s] == frozenset()


@pytest.mark.parametrize(
    "src,dst",
    [
        (RequestStatus.APPROVED, RequestStatus.REJECTED),
        (RequestStatus.RECEIVED, RequestStatus.APPROVED),
        (RequestStatus.MANUAL_REVIEW, RequestStatus.IN_PROGRESS),
        (RequestStatus.FAILED, RequestStatus.IN_PROGRESS),
    ],
)
def test_illegal_transitions_raise(src, dst):
    with pytest.raises(IllegalTransition):
        check_transition(src, dst)


def test_every_status_is_reachable_from_received():
    seen, frontier = {RequestStatus.RECEIVED}, [RequestStatus.RECEIVED]
    while frontier:
        for nxt in ALLOWED_TRANSITIONS[frontier.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    assert seen == set(RequestStatus)
