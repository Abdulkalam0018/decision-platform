"""In-memory execution of a workflow (nothing persisted), used for dry runs and impact analysis.

Uses exactly the same stage handlers as the persistent engine, so a dry run
cannot drift from production behaviour. Dependency results can be supplied
up front (e.g. the stored responses of historical requests), which makes
replaying past traffic against a candidate rule version deterministic and
free of external calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config.compiler import CompiledWorkflow
from ..config.schema import TERMINALS, DependencyStage
from ..dependencies.simulated import DependencyHub
from ..domain.models import TERMINAL_TARGET_TO_STATUS
from ..resilience.policies import AttemptRecord, PermanentDependencyError
from .engine import build_variables
from .stages import HANDLERS, StageContext, live_call


class MissingRecordedOutput(PermanentDependencyError):
    """Replay needed a dependency response that was never recorded."""


@dataclass
class SimulationResult:
    outcome: str
    path: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    needs_live_data: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def simulate(
    wf: CompiledWorkflow,
    payload: dict[str, Any],
    today: date,
    *,
    recorded_outputs: dict[str, Any] | None = None,
    hub: DependencyHub | None = None,
    run_id: str = "dryrun",
) -> SimulationResult:
    """Walk the graph in memory. If ``recorded_outputs`` is given, never call live dependencies."""
    context: dict[str, Any] = {"input": payload}
    stages = wf.config.stage_map()
    current = wf.config.start_stage
    res = SimulationResult(outcome="")

    def resolver(stage: DependencyStage, req: dict[str, Any], token: str) -> tuple[dict[str, Any], list[AttemptRecord]]:
        if recorded_outputs is not None:
            if stage.output_key in recorded_outputs:
                return recorded_outputs[stage.output_key], [AttemptRecord(attempt=0, ok=True)]
            res.needs_live_data.append(stage.output_key)
            raise MissingRecordedOutput(f"no recorded output for '{stage.output_key}'")
        if hub is None:
            raise MissingRecordedOutput("no dependency hub available for live dry run")
        return live_call(stage, req, token, sc)

    for _ in range(len(stages) + 1):
        stage = stages[current]
        meta = {
            "request_id": run_id,
            "workflow": wf.name,
            "version": wf.version,
            "retry_count": 0,
            "evaluation_date": today.isoformat(),
            "submitted_at": today.isoformat(),
        }
        sc = StageContext(
            request_id=run_id,
            workflow=wf,
            variables=build_variables(context, wf.config.parameters, meta),
            today=today,
            hub=hub,
            dependency_resolver=resolver,
        )
        result = HANDLERS[stage.type](stage, sc)
        context.update(result.context_updates)
        res.path.append(
            {"stage": stage.id, "outcome": result.outcome, "next": result.next_target, "trace": result.trace}
        )
        res.reasons.extend(result.reasons)
        res.flags.extend(result.trace.get("flags", []))
        if result.next_target is None:
            res.outcome = "RETRY_PENDING"
            break
        if result.next_target in TERMINALS:
            res.outcome = TERMINAL_TARGET_TO_STATUS[result.next_target].value
            break
        current = result.next_target
    res.context = {k: v for k, v in context.items() if k != "input"}
    return res
