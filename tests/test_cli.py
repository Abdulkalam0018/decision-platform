"""CLI smoke tests: every command runs against a real database and the shipped configs."""

import json

import pytest
from click.testing import CliRunner

from decision_platform.cli import main

from .conftest import CANDIDATES, LOAN_BASE, WORKFLOWS


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.setenv("DP_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("DP_CONFIG_DIR", str(WORKFLOWS))
    runner = CliRunner()

    def invoke(*args, code=0):
        res = runner.invoke(main, list(args), catch_exceptions=False)
        assert res.exit_code == code, res.output
        return res.output

    return invoke


def test_cli_end_to_end(run, tmp_path):
    payload = tmp_path / "loan.json"
    payload.write_text(json.dumps(LOAN_BASE))
    assert len(json.loads(run("workflows", "list"))) == 5

    out = json.loads(run("submit", "loan_application", str(payload), "--key", "cli-1"))
    rid = out["request_id"]
    assert out["status"] == "APPROVED" and out["http_status"] == 201
    assert json.loads(run("submit", "loan_application", str(payload), "--key", "cli-1"))["replayed"] is True

    assert json.loads(run("show", rid))["status"] == "APPROVED"
    assert "No rule failed" in run("explain", rid)
    assert json.loads(run("explain", rid, "--json"))["request_id"] == rid
    assert len(json.loads(run("history", rid))["stage_runs"]) == 6
    assert json.loads(run("audit", rid))[0]["event_type"] == "request.received"
    assert json.loads(run("retry", rid, code=2))["error"] == "invalid_state"
    assert json.loads(run("audit-verify"))["ok"] is True
    assert "retries" in json.loads(run("worker", "--once"))


def test_cli_review_flow(run, tmp_path):
    p = dict(LOAN_BASE, applicant=dict(LOAN_BASE["applicant"], employment_status="unemployed"))
    f = tmp_path / "rv.json"
    f.write_text(json.dumps(p))
    rid = json.loads(run("submit", "loan_application", str(f), "--key", "rv"))["request_id"]
    out = json.loads(run("review", rid, "--approve", "--reviewer", "kim", "--comment", "verified pension"))
    assert out["status"] == "APPROVED"


def test_cli_rule_change_commands(run, tmp_path):
    candidate = str(CANDIDATES / "loan_application.v2.yaml")
    assert json.loads(run("workflows", "validate", candidate))["valid"]
    p = tmp_path / "l.json"
    p.write_text(json.dumps(dict(LOAN_BASE, applicant=dict(LOAN_BASE["applicant"], id="APP-1005"))))
    run("submit", "loan_application", str(p), "--key", "pre")
    assert json.loads(run("workflows", "publish", candidate))["active_version"] == 1
    assert json.loads(run("impact", "loan_application", "--from", "1", "--to", "2"))["changed"] == 1
    assert json.loads(run("simulate", "loan_application", str(p), "--version", "2"))["outcome"] == "REJECTED"
    assert json.loads(run("workflows", "activate", "loan_application", "2"))["active_version"] == 2

    broken = tmp_path / "broken.yaml"
    broken.write_text((CANDIDATES / "loan_application.v2.yaml").read_text().replace("next: affordability", "next: x"))
    assert json.loads(run("workflows", "validate", str(broken), code=2))["error"] == "invalid_workflow"
    assert json.loads(run("show", "missing-id", code=2))["error"] == "not_found"
