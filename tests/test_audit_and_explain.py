"""Auditability: tamper-evident chain, complete event trail, explanation content."""


def test_audit_trail_for_a_request_is_complete_and_ordered(svc, loan):
    rid = svc.submit("loan_application", loan(), "au-1").body["request_id"]
    types = [e["event_type"] for e in svc.audit(rid)]
    assert types[0] == "request.received"
    assert types[-1] == "decision.made"
    assert types.count("stage.completed") == 6
    assert types.count("dependency.call_started") == 2
    assert types.count("state.changed") == 3


def test_chain_detects_modified_row(svc, loan):
    svc.submit("loan_application", loan(), "au-2")
    assert svc.verify_audit()["ok"]
    conn = svc.db.read()
    seq = conn.execute("SELECT seq FROM audit_events WHERE event_type='decision.made'").fetchone()["seq"]
    conn.execute("UPDATE audit_events SET payload=replace(payload, 'APPROVED', 'REJECTED') WHERE seq=?", (seq,))
    result = svc.verify_audit()
    assert not result["ok"] and result["first_bad_seq"] == seq and "modified" in result["problem"]


def test_chain_detects_deleted_row(svc, loan):
    svc.submit("loan_application", loan(), "au-3")
    conn = svc.db.read()
    conn.execute("DELETE FROM audit_events WHERE seq=3")
    result = svc.verify_audit()
    assert not result["ok"] and result["first_bad_seq"] == 4


def test_explanation_has_input_rules_data_and_output(svc, loan):
    rid = svc.submit("loan_application", loan(applicant={"id": "APP-1002"}), "au-4").body["request_id"]
    exp = svc.explain(rid)
    assert exp["input"]["applicant"]["id"] == "APP-1002"
    assert exp["decision"]["outcome"] == "REJECTED"
    rule = next(r for r in exp["rules_triggered"] if r["rule_id"] == "RISK-001")
    assert rule["condition"] == "credit.score >= params.min_credit_score"
    assert rule["data_refs"] == {"credit.score": 541, "params.min_credit_score": 620}
    assert exp["dependencies"][0]["response"]["score"] == 541
    assert [p["stage"] for p in exp["path"]][-1] == "risk_assessment"
    assert "[RISK-001] Minimum credit score -> FAIL (reject)" in exp["narrative"]
    assert [t["to_status"] for t in exp["lifecycle"]] == ["RECEIVED", "IN_PROGRESS", "REJECTED"]


def test_config_lifecycle_is_audited(svc):
    types = [
        r["event_type"]
        for r in svc.db.read().execute("SELECT event_type FROM audit_events WHERE request_id IS NULL ORDER BY seq")
    ]
    assert types.count("config.registered") == 5 and types.count("config.activated") == 5


def test_evaluation_date_pinned_for_reproducibility(svc, claim, clock):
    rid = svc.submit("insurance_claim", claim(), "au-5").body["request_id"]
    clock.advance(200 * 86400)  # months later the claim would be "late", but the record is unchanged
    exp = svc.explain(rid)
    assert exp["status"] == "APPROVED" and exp["evaluation_date"].startswith("2026-11-13")
