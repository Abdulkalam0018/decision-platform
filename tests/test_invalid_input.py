"""Input intake: schema validation, error detail, and auditing without storing raw payloads."""

import json

import pytest

from decision_platform.config.registry import UnknownWorkflowError
from decision_platform.service import IdempotencyKeyMissing, InputValidationError


def test_missing_required_field(svc, loan):
    payload = loan()
    del payload["applicant"]["age"]
    with pytest.raises(InputValidationError) as exc:
        svc.submit("loan_application", payload, "bad-1")
    errors = exc.value.details["errors"]
    assert errors == [{"field": "applicant", "message": "'age' is a required property", "rule": "required"}]


def test_multiple_errors_reported_with_paths(svc, loan):
    payload = loan(applicant={"age": "thirty", "email": "not-an-email"}, loan={"term_months": 2, "purpose": "yacht"})
    with pytest.raises(InputValidationError) as exc:
        svc.submit("loan_application", payload, "bad-2")
    fields = {e["field"] for e in exc.value.details["errors"]}
    assert fields == {"applicant.age", "applicant.email", "loan.term_months", "loan.purpose"}


def test_unknown_fields_rejected(svc, loan):
    with pytest.raises(InputValidationError):
        svc.submit("loan_application", loan(applicant={"is_admin": True}), "bad-3")


@pytest.mark.parametrize("payload", [None, [], "string", 42])
def test_non_object_payload(svc, payload):
    with pytest.raises(InputValidationError):
        svc.submit("loan_application", payload, "bad-4")


def test_invalid_input_creates_no_request_but_is_audited_without_raw_payload(svc, loan):
    payload = loan(applicant={"age": -1, "name": "Secret Name"})
    with pytest.raises(InputValidationError):
        svc.submit("loan_application", payload, "bad-5")
    conn = svc.db.read()
    assert conn.execute("SELECT COUNT(*) c FROM requests").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM idempotency_keys").fetchone()["c"] == 0
    row = conn.execute("SELECT payload FROM audit_events WHERE event_type='intake.rejected'").fetchone()
    stored = json.loads(row["payload"])
    assert stored["errors"][0]["field"] == "applicant.age"
    assert "payload_sha256" in stored and "Secret Name" not in row["payload"]


def test_same_key_can_be_reused_after_fixing_invalid_input(svc, loan):
    bad = loan(applicant={"age": -1})
    with pytest.raises(InputValidationError):
        svc.submit("loan_application", bad, "fix-me")
    assert svc.submit("loan_application", loan(), "fix-me").http_status == 201


def test_unknown_workflow(svc, loan):
    with pytest.raises(UnknownWorkflowError):
        svc.submit("mortgage_magic", loan(), "k")


@pytest.mark.parametrize("key", [None, "", "   ", "x" * 256])
def test_idempotency_key_required(svc, loan, key):
    with pytest.raises(IdempotencyKeyMissing):
        svc.submit("loan_application", loan(), key)
