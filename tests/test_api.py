"""End-to-end over HTTP with FastAPI's TestClient."""

import pytest
from fastapi.testclient import TestClient

from decision_platform.api.app import create_app

from .conftest import CANDIDATES


@pytest.fixture
def client(svc):
    return TestClient(create_app(svc), raise_server_exceptions=False)


def post(client, payload, key="api-1", wf="loan_application"):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post(f"/v1/workflows/{wf}/requests", json=payload, headers=headers)


def test_submit_and_replay(client, loan):
    r1 = post(client, loan())
    assert r1.status_code == 201 and r1.json()["status"] == "APPROVED"
    assert r1.headers["Location"] == f"/v1/requests/{r1.json()['request_id']}"
    r2 = post(client, loan())
    assert r2.status_code == 200 and r2.headers["Idempotent-Replayed"] == "true" and r2.json()["replayed"]


def test_error_responses_are_problem_json(client, loan):
    bad = loan(applicant={"age": "x"})
    r = post(client, bad)
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["code"] == "invalid_input" and r.json()["errors"][0]["field"] == "applicant.age"

    assert post(client, loan(), key=None).status_code == 400
    assert post(client, loan(), wf="nope").status_code == 404
    post(client, loan(), key="dup")
    reuse = post(client, loan(loan={"amount": 1}), key="dup")
    assert reuse.status_code == 422 and reuse.json()["code"] == "idempotency_key_reused"
    assert client.get("/v1/requests/does-not-exist").status_code == 404


def test_read_endpoints(client, loan):
    rid = post(client, loan(applicant={"id": "APP-1002"})).json()["request_id"]
    assert client.get(f"/v1/requests/{rid}").json()["status"] == "REJECTED"
    assert len(client.get(f"/v1/requests/{rid}/history").json()["stage_runs"]) == 5
    assert client.get(f"/v1/requests/{rid}/audit").json()[0]["event_type"] == "request.received"
    exp = client.get(f"/v1/requests/{rid}/explanation").json()
    assert exp["rules_triggered"][0]["rule_id"] == "RISK-001"
    text = client.get(f"/v1/requests/{rid}/explanation?format=text")
    assert text.headers["content-type"].startswith("text/plain") and "RISK-001" in text.text
    assert client.get("/v1/requests?status=REJECTED").json()[0]["request_id"] == rid
    assert client.get("/v1/admin/audit/verify").json()["ok"]


def test_fault_injection_retry_and_review_over_http(client, loan, clock):
    r = client.put("/v1/admin/dependencies/credit_bureau/faults", json={"fail_next": ["transient"] * 3})
    assert r.status_code == 200
    rid = post(client, loan(), key="flow").json()["request_id"]
    assert client.get(f"/v1/requests/{rid}").json()["status"] == "RETRY_PENDING"
    assert client.post(f"/v1/requests/{rid}/retry").json()["status"] == "APPROVED"
    assert client.post(f"/v1/requests/{rid}/retry").status_code == 409

    rv = post(client, loan(applicant={"id": "APP-7", "employment_status": "unemployed"}), key="rv").json()
    assert rv["status"] == "MANUAL_REVIEW"
    out = client.post(
        f"/v1/requests/{rv['request_id']}/review",
        json={"decision": "reject", "reviewer": "kim", "comment": "unverifiable income"},
    )
    assert out.json()["status"] == "REJECTED"
    assert client.put("/v1/admin/dependencies/nope/faults", json={}).status_code == 404


def test_publish_activate_simulate_impact_over_http(client, loan):
    post(client, loan(applicant={"id": "APP-1005"}), key="pre")
    yaml_text = (CANDIDATES / "loan_application.v2.yaml").read_text()
    assert client.post("/v1/workflows/validate", content=yaml_text).json()["valid"]
    pub = client.post("/v1/workflows?actor=risk&reason=policy", content=yaml_text)
    assert pub.status_code == 201 and pub.json()["version"] == 2
    impact = client.get("/v1/workflows/loan_application/impact?from_version=1&to_version=2").json()
    assert impact["changed"] == 1
    sim = client.post(
        "/v1/workflows/loan_application/simulate", json={"input": loan(applicant={"id": "APP-1005"}), "version": 2}
    ).json()
    assert sim["outcome"] == "REJECTED"
    act = client.post("/v1/workflows/loan_application/versions/2/activate?actor=risk")
    assert act.json()["active_version"] == 2
    detail = client.get("/v1/workflows/loan_application").json()
    assert detail["active_version"] == 2 and len(detail["activations"]) == 2

    broken = yaml_text.replace("version: 2", "version: 3").replace("next: affordability", "next: nowhere")
    bad = client.post("/v1/workflows", content=broken)
    assert bad.status_code == 422 and any("nowhere" in e for e in bad.json()["errors"])
    tampered = yaml_text.replace("min_credit_score: 650", "min_credit_score: 1")
    assert client.post("/v1/workflows", content=tampered).status_code == 409


def test_ops_endpoints(client, loan, clock):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["workflows"] == 5
    client.put("/v1/admin/dependencies/credit_bureau/faults", json={"fail_next": ["transient"] * 3})
    post(client, loan(), key="ops")
    clock.advance(3600)
    assert client.post("/v1/admin/retries/run").json()["processed"] == 1
    assert client.post("/v1/admin/outbox/relay").json()["pending"] == 0
    assert "credit_bureau" in client.get("/v1/admin/dependencies").json()
    assert client.post("/v1/admin/dependencies/reset").status_code == 200


def test_unexpected_error_returns_500_problem(client, svc, loan, monkeypatch):
    monkeypatch.setattr(svc.engine, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = post(client, loan(), key="crash")
    assert r.status_code == 500 and r.json()["code"] == "internal_error"
