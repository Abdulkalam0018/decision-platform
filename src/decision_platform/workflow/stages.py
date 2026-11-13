"""Stage handlers.

A handler takes a stage definition and a read-only view of the request
context and returns a ``StageResult``. Handlers never touch the database: the
engine owns persistence and state transitions. Adding a new stage type means
adding one handler and one pydantic model; the engine does not change.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config.compiler import CompiledWorkflow
from ..config.schema import BranchStage, ComputeStage, DependencyStage, RulesStage
from ..dependencies.simulated import DependencyHub
from ..domain.models import StageResult
from ..resilience.policies import (
    AttemptRecord,
    BackoffPolicy,
    CircuitOpenError,
    PermanentDependencyError,
    TransientDependencyError,
    call_with_retry,
)
from ..rules.engine import EVAL_ERRORS, evaluate_rules
from ..rules.expression import EvalContext, MissingDataError

DependencyResolver = Callable[[DependencyStage, dict[str, Any], str], tuple[dict[str, Any], list[AttemptRecord]]]


@dataclass
class StageContext:
    request_id: str
    workflow: CompiledWorkflow
    variables: dict[str, Any]
    today: date
    hub: DependencyHub | None = None
    sleep: Callable[[float], None] = lambda s: None
    rng: random.Random = field(default_factory=lambda: random.Random(42))
    # Optional override used by dry-runs / impact analysis to avoid live calls.
    dependency_resolver: DependencyResolver | None = None


def run_rules(stage: RulesStage, sc: StageContext) -> StageResult:
    outcome, traces = evaluate_rules(stage, sc.workflow, sc.variables, sc.today)
    reasons = [t.reason for t in traces if t.action in ("reject", "review") and t.reason]
    flags = [t.reason for t in traces if t.action == "flag" and t.reason]
    return StageResult(
        outcome=outcome,
        next_target=stage.targets()[outcome],
        trace={
            "rules": [t.to_dict() for t in traces],
            "summary": {
                "evaluated": sum(1 for t in traces if t.outcome.value != "SKIPPED"),
                "passed": sum(1 for t in traces if t.outcome.value == "PASS"),
                "failed": sum(1 for t in traces if t.outcome.value in ("FAIL", "MISSING_DATA", "ERROR")),
                "skipped": sum(1 for t in traces if t.outcome.value == "SKIPPED"),
            },
            "flags": flags,
        },
        reasons=reasons,
    )


def run_compute(stage: ComputeStage, sc: StageContext) -> StageResult:
    derived: dict[str, Any] = {}
    refs: dict[str, Any] = {}
    for name, expr in stage.outputs.items():
        ctx = EvalContext(sc.variables, sc.today)
        try:
            derived[name] = sc.workflow.expr(expr).evaluate(ctx)
        except MissingDataError as exc:
            return StageResult(
                "error",
                "manual_review",
                trace={"failed_output": name, "expression": expr, "missing": exc.path},
                reasons=[f"Could not compute '{name}': required data '{exc.path}' is missing"],
            )
        except EVAL_ERRORS as exc:
            return StageResult(
                "error",
                "manual_review",
                trace={"failed_output": name, "expression": expr, "error": str(exc)},
                reasons=[f"Could not compute '{name}': {type(exc).__name__}: {exc}"],
            )
        refs.update(ctx.reads)
    merged = {**sc.variables.get("derived", {}), **derived}
    return StageResult(
        "pass",
        stage.next,
        context_updates={"derived": merged},
        trace={"outputs": derived, "data_refs": dict(sorted(refs.items()))},
    )


def run_branch(stage: BranchStage, sc: StageContext) -> StageResult:
    evaluated: list[dict[str, Any]] = []
    for i, br in enumerate(stage.branches):
        ctx = EvalContext(sc.variables, sc.today)
        try:
            hit = bool(sc.workflow.expr(br.when).evaluate(ctx))
        except MissingDataError as exc:
            return StageResult(
                "error",
                "manual_review",
                trace={"branches": evaluated, "missing": exc.path},
                reasons=[f"Routing condition needs missing data '{exc.path}'"],
            )
        except EVAL_ERRORS as exc:
            return StageResult(
                "error",
                "manual_review",
                trace={"branches": evaluated, "failed_condition": br.when, "error": f"{type(exc).__name__}: {exc}"},
                reasons=[f"Routing condition '{br.when}' could not be evaluated: {type(exc).__name__}"],
            )
        evaluated.append(
            {
                "index": i,
                "label": br.label,
                "when": br.when,
                "matched": hit,
                "data_refs": dict(sorted(ctx.reads.items())),
            }
        )
        if hit:
            return StageResult(f"branch[{i}]", br.next, trace={"branches": evaluated, "selected": br.label or br.next})
    return StageResult("default", stage.default, trace={"branches": evaluated, "selected": "default"})


def live_call(
    stage: DependencyStage, payload: dict[str, Any], token: str, sc: StageContext
) -> tuple[dict[str, Any], list[AttemptRecord]]:
    assert sc.hub is not None
    dep_cfg = sc.workflow.config.dependencies[stage.dependency]
    adapter = sc.hub.get(dep_cfg.type)
    breaker = sc.hub.breaker(
        f"{sc.workflow.name}:{stage.dependency}",
        dep_cfg.circuit_breaker.failure_threshold,
        dep_cfg.circuit_breaker.reset_timeout_s,
        type_name=dep_cfg.type,
    )
    policy = BackoffPolicy(
        dep_cfg.retry.max_attempts, dep_cfg.retry.base_delay_ms / 1000, dep_cfg.retry.max_delay_ms / 1000
    )
    result, outcome = call_with_retry(
        lambda: breaker.call(lambda: adapter.invoke(payload, token, dep_cfg.timeout_ms)),
        policy,
        sleep=sc.sleep,
        rng=sc.rng,
    )
    return result, outcome.attempts


def run_dependency(stage: DependencyStage, sc: StageContext) -> StageResult:
    payload: dict[str, Any] = {}
    for key, expr in stage.request.items():
        ctx = EvalContext(sc.variables, sc.today)
        try:
            payload[key] = sc.workflow.expr(expr).evaluate(ctx)
        except MissingDataError as exc:
            return StageResult(
                "error",
                stage.on_error,
                trace={"dependency": stage.dependency, "missing": exc.path},
                reasons=[f"Cannot call {stage.dependency}: required data '{exc.path}' is missing"],
            )
        except EVAL_ERRORS as exc:
            return StageResult(
                "error",
                stage.on_error,
                trace={"dependency": stage.dependency, "field": key, "error": f"{type(exc).__name__}: {exc}"},
                reasons=[f"Cannot build {stage.dependency} request field '{key}': {type(exc).__name__}"],
            )

    token = f"{sc.request_id}:{stage.id}"
    base_trace: dict[str, Any] = {"dependency": stage.dependency, "idempotency_token": token, "request": payload}
    resolver = sc.dependency_resolver or (lambda st, p, t: live_call(st, p, t, sc))
    try:
        result, attempts = resolver(stage, payload, token)
    except PermanentDependencyError as exc:
        outcome = getattr(exc, "retry_outcome", None)
        return StageResult(
            "error",
            stage.on_error,
            trace={
                **base_trace,
                "error": str(exc),
                "error_class": "permanent",
                "attempts": [a.__dict__ for a in outcome.attempts] if outcome else [],
            },
            reasons=[f"{stage.dependency} returned a non-retryable error: {exc}"],
        )
    except TransientDependencyError as exc:
        outcome = getattr(exc, "retry_outcome", None)
        target = None if stage.on_unavailable == "retry_later" else stage.on_unavailable
        return StageResult(
            "unavailable",
            target,
            trace={
                **base_trace,
                "error": str(exc),
                "error_class": "circuit_open" if isinstance(exc, CircuitOpenError) else "transient",
                "attempts": [a.__dict__ for a in outcome.attempts] if outcome else [],
            },
            reasons=[f"{stage.dependency} unavailable after retries: {exc}"],
        )
    return StageResult(
        "pass",
        stage.next,
        context_updates={stage.output_key: result},
        trace={**base_trace, "response": result, "attempts": [a.__dict__ for a in attempts]},
    )


HANDLERS: dict[str, Callable[[Any, StageContext], StageResult]] = {
    "rules": run_rules,
    "compute": run_compute,
    "branch": run_branch,
    "dependency": run_dependency,
}
