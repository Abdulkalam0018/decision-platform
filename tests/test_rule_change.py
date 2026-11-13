"""Rule change scenarios: versioned activation, pinning, rollback, dry runs and impact analysis."""

import pytest
import yaml

from decision_platform.config.compiler import WorkflowValidationError

from .conftest import CANDIDATES


def v2_raw():
    return yaml.safe_load((CANDIDATES / "loan_application.v2.yaml").read_text())


def publish_v2(svc, activate=True):
    return svc.registry.register(v2_raw(), actor="risk-team", activate=activate, reason="tighten credit policy")


def test_new_requests_use_new_rules_after_activation(svc, loan):
    before = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "rc-1").body
    assert before["status"] == "APPROVED" and before["workflow_version"] == 1

    publish_v2(svc)
    after = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}, loan={"amount": 600001}), "rc-2").body
    assert after["workflow_version"] == 2 and after["status"] == "REJECTED"
    assert "Credit score 634 is below the minimum 650" in after["decision"]["reasons"]


def test_historical_decision_and_explanation_unchanged(svc, loan):
    old = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "rc-3").body
    publish_v2(svc)
    exp = svc.explain(old["request_id"])
    assert exp["status"] == "APPROVED" and exp["workflow_version"] == 1
    assert exp["definition_hash"] == svc.registry.get("loan_application", 1).content_hash


def test_in_flight_request_finishes_on_its_pinned_version(svc, loan):
    svc.hub.get("credit_bureau").configure(fail_next=["transient"] * 3)
    parked = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "rc-4").body
    assert parked["status"] == "RETRY_PENDING"
    publish_v2(svc)
    final = svc.retry(parked["request_id"])
    assert final["workflow_version"] == 1 and final["status"] == "APPROVED"  # v1 threshold 620 still applies


def test_new_conditional_rule_only_in_v2(svc, loan):
    young = loan(applicant={"age": 19}, loan={"amount": 300000, "term_months": 36})
    assert svc.simulate("loan_application", young, version=1)["outcome"] == "APPROVED"
    publish_v2(svc)
    res = svc.submit("loan_application", young, "rc-5").body
    assert res["status"] == "MANUAL_REVIEW"
    assert any("cap for under-21" in r for r in res["decision"]["reasons"])


def test_rollback_by_reactivating_previous_version(svc, loan):
    publish_v2(svc)
    svc.registry.activate("loan_application", 1, actor="oncall", reason="rollback: false rejects")
    res = svc.submit("loan_application", loan(applicant={"id": "APP-1005"}), "rc-6").body
    assert res["workflow_version"] == 1 and res["status"] == "APPROVED"
    history = svc.registry.activation_history("loan_application")
    assert [h["version"] for h in history] == [1, 2, 1]
    assert history[-1]["reason"] == "rollback: false rejects"


def test_publish_without_activation_keeps_current_version(svc):
    publish_v2(svc, activate=False)
    assert svc.registry.active_version("loan_application") == 1
    assert {(w["version"], w["active"]) for w in svc.registry.list_versions() if w["name"] == "loan_application"} == {
        (1, True),
        (2, False),
    }


def test_invalid_new_version_never_becomes_active(svc):
    raw = v2_raw()
    raw["version"] = 3
    raw["stages"][4]["rules"][0]["condition"] = "credit.score >= params.min_score_typo"
    with pytest.raises(WorkflowValidationError, match="unknown parameter 'min_score_typo'"):
        svc.registry.register(raw, actor="risk-team", activate=True)
    assert svc.registry.active_version("loan_application") == 1


def test_dry_run_has_no_side_effects(svc, loan):
    publish_v2(svc, activate=False)
    out = svc.simulate("loan_application", loan(applicant={"id": "APP-1005"}), version=2)
    assert out["dry_run"] and out["outcome"] == "REJECTED"
    assert svc.db.read().execute("SELECT COUNT(*) c FROM requests").fetchone()["c"] == 0


def test_dry_run_with_recorded_outputs_makes_no_external_calls(svc, loan):
    out = svc.simulate(
        "loan_application",
        loan(),
        recorded_outputs={"credit": {"score": 600, "open_defaults": 0}, "fraud": {"risk_score": 3}},
    )
    assert out["outcome"] == "REJECTED"
    assert svc.hub.get("credit_bureau").calls == 0


def test_impact_analysis_before_activation(svc, loan):
    ids = {}
    for key, app_id in [("i-1", "APP-1001"), ("i-2", "APP-1005"), ("i-3", "APP-1002"), ("i-4", "APP-1003")]:
        ids[app_id] = svc.submit("loan_application", loan(applicant={"id": app_id}), key).body["request_id"]
    publish_v2(svc, activate=False)
    calls_before = svc.hub.get("credit_bureau").calls
    report = svc.impact("loan_application", 1, 2)
    assert report["replayed"] == 4 and report["changed"] == 1
    assert report["changes"][0]["request_id"] == ids["APP-1005"]
    assert report["changes"][0]["before"] == "APPROVED" and report["changes"][0]["after"] == "REJECTED"
    assert report["by_transition"]["APPROVED->APPROVED"] == 2
    assert svc.hub.get("credit_bureau").calls == calls_before  # replay used recorded responses only
