"""Rules evaluation: mandatory, threshold, conditional, flags, missing data, branching, multi-step."""

import copy

import yaml

from .conftest import KYC_BASE, ONBOARDING_BASE, VENDOR_BASE, WORKFLOWS, deep_merge


def triggered(svc, rid):
    return {r["rule_id"]: r for r in svc.explain(rid)["rules_triggered"]}


def test_reject_collects_all_failing_rules(svc, loan):
    res = svc.submit("loan_application", loan(applicant={"age": 16}, loan={"amount": 9_000_000}), "r-1").body
    assert res["status"] == "REJECTED"
    t = triggered(svc, res["request_id"])
    assert set(t) == {"ELIG-001", "ELIG-003"}
    assert t["ELIG-001"]["reason"] == "Applicant age 16 is below the minimum age 18"
    assert t["ELIG-001"]["data_refs"] == {"input.applicant.age": 16, "params.min_age": 18}
    assert res["decision"]["decided_at_stage"] == "eligibility"
    # rejected at eligibility: no external calls were made
    assert svc.hub.get("credit_bureau").side_effects == 0


def test_review_outcome_for_unemployed(svc, loan):
    res = svc.submit("loan_application", loan(applicant={"employment_status": "unemployed"}), "r-2").body
    assert res["status"] == "MANUAL_REVIEW"
    assert "ELIG-005" in triggered(svc, res["request_id"])


def test_reject_beats_review_when_both_fail(svc, loan):
    res = svc.submit("loan_application", loan(applicant={"employment_status": "unemployed", "age": 17}), "r-3").body
    assert res["status"] == "REJECTED"
    assert {"ELIG-001", "ELIG-005"} <= set(triggered(svc, res["request_id"]))


def test_flag_is_recorded_but_non_blocking(svc, loan):
    res = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "r-4").body  # score 634
    assert res["status"] == "APPROVED"
    assert res["decision"]["flags"] == ["Credit score 634 is below the prime band 680 (non-blocking)"]


def test_conditional_rule_skipped_when_not_applicable(svc, loan):
    rid = svc.submit("loan_application", loan(), "r-5").body["request_id"]
    exp = svc.explain(rid)
    assert "ELIG-006" in exp["rules_not_applicable"]  # not self-employed
    assert "RISK-005" in exp["rules_not_applicable"]  # no collateral -> ltv is None


def test_missing_optional_data_uses_on_missing(svc, loan):
    payload = loan(applicant={"employment_status": "self_employed"})
    res = svc.submit("loan_application", payload, "r-6").body
    assert res["status"] == "MANUAL_REVIEW"
    t = triggered(svc, res["request_id"])["ELIG-006"]
    assert t["outcome"] == "MISSING_DATA" and t["action"] == "review"
    assert "input.applicant.years_employed" in t["reason"]


def test_threshold_reject_from_dependency_data(svc, loan):
    res = svc.submit("loan_application", loan(applicant={"id": "APP-1002"}), "r-7").body  # score 541, 2 defaults
    assert res["status"] == "REJECTED"
    t = triggered(svc, res["request_id"])
    assert t["RISK-001"]["data_refs"] == {"credit.score": 541, "params.min_credit_score": 620}
    assert t["RISK-002"]["action"] == "review"


def test_branch_routes_elevated_dti_to_manual_review(svc, loan):
    # dti = (40000 + 600000/60) / 120000 = 0.4167 > review_dti 0.36 but <= max 0.45
    res = svc.submit("loan_application", loan(applicant={"monthly_income": 120000, "monthly_debt": 40000}), "r-8").body
    assert res["status"] == "MANUAL_REVIEW"
    assert svc.explain(res["request_id"])["routing"][0]["selected"] == "elevated debt-to-income"


def test_high_value_multi_step_path(svc, loan):
    ok = svc.submit(
        "loan_application",
        loan(
            applicant={"monthly_income": 900000},
            loan={"amount": 2_000_000, "term_months": 120, "collateral_value": 3_000_000},
        ),
        "hv-1",
    ).body
    assert ok["status"] == "APPROVED"
    assert ok["decision"]["path"][-1] == "high_value_checks"

    no_collateral = svc.submit(
        "loan_application",
        loan(applicant={"id": "APP-1003", "monthly_income": 900000}, loan={"amount": 2_000_000, "term_months": 120}),
        "hv-2",
    ).body
    assert no_collateral["status"] == "MANUAL_REVIEW"
    assert {"HV-001", "HV-002"} <= set(triggered(svc, no_collateral["request_id"]))


def test_ltv_conditional_rule_applies_with_collateral(svc, loan):
    res = svc.submit("loan_application", loan(loan={"collateral_value": 650000}), "ltv-1").body
    assert res["status"] == "MANUAL_REVIEW"
    assert triggered(svc, res["request_id"])["RISK-005"]["data_refs"]["derived.ltv"] == round(600000 / 650000, 4)


def test_claims_date_rules_use_pinned_evaluation_date(svc, claim):
    late = svc.submit("insurance_claim", claim(claim_id="CLM-late", incident={"date": "2026-07-01"}), "c-late").body
    assert late["status"] == "REJECTED"
    assert "CLM-002" in triggered(svc, late["request_id"])
    future = svc.submit("insurance_claim", claim(claim_id="CLM-fut", incident={"date": "2026-12-01"}), "c-fut").body
    assert "CLM-001" in triggered(svc, future["request_id"])


def test_claims_theft_without_police_report(svc, claim):
    res = svc.submit("insurance_claim", claim(claim_id="CLM-t", incident={"type": "theft"}), "c-theft").body
    assert res["status"] == "REJECTED"
    assert triggered(svc, res["request_id"])["CLM-003"]["reason"] == "Theft claims require a police report number"


def test_claims_payout_branches(svc, claim):
    big = svc.submit("insurance_claim", claim(claim_id="CLM-big", amount_claimed=120000), "c-big").body
    assert big["status"] == "MANUAL_REVIEW"
    tiny = svc.submit("insurance_claim", claim(claim_id="CLM-tiny", amount_claimed=3000), "c-tiny").body
    assert tiny["status"] == "REJECTED"  # 3000 - 5000 deductible -> nothing payable


def test_claims_inactive_policy_and_fake_document(svc, claim):
    inactive = svc.submit("insurance_claim", claim(claim_id="CLM-in", policy_number="POL-404"), "c-in").body
    assert "COV-001" in triggered(svc, inactive["request_id"])
    fake = svc.submit(
        "insurance_claim", claim(claim_id="CLM-fk", documents=[{"type": "id", "number": "FAKE-1"}]), "c-fk"
    ).body
    assert fake["status"] == "MANUAL_REVIEW"


def test_onboarding_work_permit_and_privileged_access(svc):
    foreign = deep_merge(ONBOARDING_BASE, {"candidate": {"id": "CAND-777", "country": "US"}})
    res = svc.submit("employee_onboarding", foreign, "onb-f").body
    assert res["status"] == "REJECTED" and "ONB-003" in triggered(svc, res["request_id"])

    priv = deep_merge(ONBOARDING_BASE, {"candidate": {"id": "CAND-888"}, "requested_access": ["email", "prod_admin"]})
    assert svc.submit("employee_onboarding", priv, "onb-p").body["status"] == "MANUAL_REVIEW"


def test_vendor_sanctions_first_failure_and_committee(svc):
    sanctioned = deep_merge(VENDOR_BASE, {"vendor": {"name": "Sanctioned Trading LLC", "tax_id": "27AAACS9999K1Z2"}})
    res = svc.submit("vendor_approval", sanctioned, "v-s").body
    assert res["status"] == "REJECTED" and "SAN-001" in triggered(svc, res["request_id"])

    bad_tax = deep_merge(VENDOR_BASE, {"vendor": {"tax_id": "NOT-A-GSTIN"}})
    assert svc.submit("vendor_approval", bad_tax, "v-t").body["status"] == "REJECTED"

    big = deep_merge(VENDOR_BASE, {"vendor": {"tax_id": "29AAACB7777L1Z9", "annual_contract_value": 5_000_000}})
    assert svc.submit("vendor_approval", big, "v-b").body["status"] == "MANUAL_REVIEW"


def test_kyc_expiring_document_rejected(svc):
    soon = copy.deepcopy(KYC_BASE)
    soon["documents"][1]["expiry_date"] = "2026-11-20"
    res = svc.submit("document_verification", soon, "kyc-x").body
    assert res["status"] == "REJECTED" and "KYC-002" in triggered(svc, res["request_id"])


def test_rule_evaluation_error_is_contained(svc, loan):
    """A type error inside a rule never crashes the engine; it becomes a traced ERROR outcome."""
    raw = yaml.safe_load((WORKFLOWS / "loan_application.v1.yaml").read_text())
    raw["version"] = 10
    raw["stages"][0]["rules"][0]["condition"] = "input.applicant.name > 5"  # str > int -> TypeError
    svc.registry.register(raw, actor="test", activate=True)
    res = svc.submit("loan_application", loan(), "err-1").body
    assert res["status"] == "REJECTED"
    t = triggered(svc, res["request_id"])["ELIG-001"]
    assert t["outcome"] == "ERROR" and "TypeError" in t["detail"]
