"""Configuration model: shipped workflows are valid, broken ones are rejected with precise errors."""

import copy
from pathlib import Path

import pytest
import yaml

from decision_platform.config.compiler import WorkflowValidationError, compile_workflow
from decision_platform.config.registry import VersionConflictError
from decision_platform.dependencies.simulated import DependencyHub

from .conftest import CANDIDATES, WORKFLOWS

TYPES = DependencyHub().types


def load(name: str, directory: Path = WORKFLOWS) -> dict:
    return yaml.safe_load((directory / name).read_text())


@pytest.mark.parametrize(
    "path", sorted(WORKFLOWS.glob("*.yaml")) + sorted(CANDIDATES.glob("*.yaml")), ids=lambda p: p.name
)
def test_all_shipped_workflows_compile(path):
    wf = compile_workflow(yaml.safe_load(path.read_text()), TYPES)
    assert wf.content_hash and wf.input_validator is not None


def mutate(fn):
    raw = copy.deepcopy(load("loan_application.v1.yaml"))
    fn(raw)
    return raw


def errors_for(raw):
    with pytest.raises(WorkflowValidationError) as exc:
        compile_workflow(raw, TYPES)
    return "\n".join(exc.value.errors)


def stage(raw, sid):
    return next(s for s in raw["stages"] if s["id"] == sid)


def test_unknown_transition_target():
    raw = mutate(lambda r: stage(r, "eligibility").update(next="nowhere"))
    assert "unknown target 'nowhere'" in errors_for(raw)


def test_cycle_detected():
    raw = mutate(lambda r: stage(r, "high_value_checks").update(next="eligibility"))
    assert "cycle" in errors_for(raw)


def test_unreachable_stage_detected():
    def f(r):
        r["stages"].append({"id": "orphan", "type": "compute", "outputs": {"x": "1"}, "next": "approve"})

    assert "unreachable stages: ['orphan']" in errors_for(mutate(f))


def test_unknown_parameter_detected():
    raw = mutate(lambda r: r["parameters"].pop("min_credit_score"))
    assert "unknown parameter 'min_credit_score'" in errors_for(raw)


def test_undeclared_dependency_and_unregistered_type():
    def f(r):
        r["dependencies"]["credit_bureau"]["type"] = "astrology_api"
        stage(r, "fraud_check")["dependency"] = "ghost"

    errs = errors_for(mutate(f))
    assert "unregistered adapter type 'astrology_api'" in errs
    assert "undeclared dependency 'ghost'" in errs


def test_invalid_expression_detected_at_load_time():
    raw = mutate(lambda r: stage(r, "eligibility")["rules"][0].update(condition="__import__('os')"))
    assert "ELIG-001.condition" in errors_for(raw)


def test_data_flow_violation_reading_dependency_before_it_runs():
    raw = mutate(lambda r: stage(r, "eligibility")["rules"][0].update(condition="credit.score > 1"))
    assert "reads 'credit.score' but 'credit' is not produced on every path" in errors_for(raw)


def test_data_flow_violation_derived_value_not_computed():
    raw = mutate(lambda r: stage(r, "eligibility")["rules"][0].update(condition="derived.dti < 1"))
    assert "'derived.dti' is not produced on every path" in errors_for(raw)


def test_duplicate_rule_ids():
    raw = mutate(lambda r: stage(r, "risk_assessment")["rules"][1].update(id="RISK-001"))
    assert "duplicate rule id 'RISK-001'" in errors_for(raw)


def test_invalid_input_json_schema():
    raw = mutate(lambda r: r["input_schema"].update(type="not-a-type"))
    assert "input_schema is not a valid JSON Schema" in errors_for(raw)


def test_reserved_stage_id_and_conditional_without_when():
    def f(r):
        stage(r, "routing")["id"] = "approve"

    assert "reserved" in errors_for(mutate(f))

    def g(r):
        stage(r, "eligibility")["rules"][3].pop("when")

    assert "conditional rules require a 'when' clause" in errors_for(mutate(g))


def test_unknown_fields_rejected():
    raw = mutate(lambda r: stage(r, "eligibility").update(nxt="typo"))
    assert "Extra inputs are not permitted" in errors_for(raw)


def test_all_errors_reported_together():
    def f(r):
        r["parameters"].pop("min_age")
        stage(r, "eligibility").update(next="nowhere")
        stage(r, "risk_assessment")["rules"][0].update(condition="credit.score >=")

    errs = errors_for(mutate(f))
    assert "min_age" in errs and "nowhere" in errs and "syntax error" in errs


def test_versions_are_immutable(svc):
    raw = load("loan_application.v1.yaml")
    svc.registry.register(raw, actor="test")  # identical content: idempotent no-op
    changed = copy.deepcopy(raw)
    changed["parameters"]["min_credit_score"] = 500
    with pytest.raises(VersionConflictError):
        svc.registry.register(changed, actor="test")
    assert svc.registry.active("loan_application").config.parameters["min_credit_score"] == 620


def test_rejected_config_is_audited_and_not_activated(svc):
    raw = load("loan_application.v2.yaml", CANDIDATES)
    raw["version"] = 3
    stage(raw, "eligibility")["next"] = "nowhere"
    with pytest.raises(WorkflowValidationError):
        svc.registry.register(raw, actor="alice", activate=True)
    assert svc.registry.active_version("loan_application") == 1
    rows = svc.db.read().execute("SELECT payload FROM audit_events WHERE event_type='config.rejected'").fetchall()
    assert len(rows) == 1 and "nowhere" in rows[0]["payload"]


def test_registry_persists_across_restart(svc, db, clock):
    from decision_platform.service import DecisionService

    svc.registry.register(load("loan_application.v2.yaml", CANDIDATES), actor="ops", activate=True)
    restarted = DecisionService(db, clock=clock)
    restarted.load_workflows(WORKFLOWS)  # bootstrap must not override an operator's activation
    assert restarted.registry.active_version("loan_application") == 2
