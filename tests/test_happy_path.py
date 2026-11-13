"""Happy paths for every shipped workflow plus lifecycle bookkeeping."""

from .conftest import KYC_BASE, ONBOARDING_BASE, VENDOR_BASE, statuses


def test_loan_happy_path_approved(svc, loan):
    res = svc.submit("loan_application", loan(), "happy-1")
    assert res.http_status == 201
    body = res.body
    assert body["status"] == "APPROVED"
    assert body["decision"]["decided_by"] == "engine"
    assert body["decision"]["path"] == [
        "eligibility",
        "affordability",
        "credit_check",
        "fraud_check",
        "risk_assessment",
        "routing",
    ]
    assert statuses(svc, body["request_id"]) == ["RECEIVED", "IN_PROGRESS", "APPROVED"]


def test_stage_runs_and_context_persisted(svc, loan):
    rid = svc.submit("loan_application", loan(), "happy-2").body["request_id"]
    runs = svc.history(rid)["stage_runs"]
    assert [r["stage_id"] for r in runs] == [
        "eligibility",
        "affordability",
        "credit_check",
        "fraud_check",
        "risk_assessment",
        "routing",
    ]
    credit = next(r for r in runs if r["stage_id"] == "credit_check")
    assert credit["context_patch"]["credit"]["score"] == 782
    affordability = next(r for r in runs if r["stage_id"] == "affordability")
    assert affordability["trace"]["outputs"]["dti"] == round(10000 / 150000 + 10000 / 150000, 4)


def test_decision_emitted_to_outbox_and_audit_chain_valid(svc, loan):
    svc.submit("loan_application", loan(), "happy-3")
    out = svc.db.read().execute("SELECT event_type FROM outbox").fetchall()
    assert [r["event_type"] for r in out] == ["decision.made"]
    assert svc.verify_audit()["ok"]


def test_claim_happy_path(svc, claim):
    res = svc.submit("insurance_claim", claim(), "claim-1").body
    assert res["status"] == "APPROVED"
    exp = svc.explain(res["request_id"])
    assert exp["derived_values"]["payable"] == 37000  # 42000 - 5000 deductible


def test_onboarding_happy_path(svc):
    assert svc.submit("employee_onboarding", ONBOARDING_BASE, "onb-1").body["status"] == "APPROVED"


def test_vendor_happy_path(svc):
    assert svc.submit("vendor_approval", VENDOR_BASE, "vnd-1").body["status"] == "APPROVED"


def test_kyc_happy_path(svc):
    assert svc.submit("document_verification", KYC_BASE, "kyc-1").body["status"] == "APPROVED"


def test_list_requests_filters(svc, loan, claim):
    svc.submit("loan_application", loan(), "l-1")
    svc.submit("insurance_claim", claim(), "c-1")
    assert len(svc.list_requests(workflow="insurance_claim")) == 1
    assert len(svc.list_requests(status="APPROVED")) == 2
