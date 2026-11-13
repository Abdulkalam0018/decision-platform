"""Workflow execution engine.

The engine walks the stage graph one stage at a time. After every stage it
commits a *checkpoint* in a single transaction:

    stage_run record + context patch + request row (CAS on lock_version)
    + transition history + audit events + outbox message (if terminal)

Consequences:
* A crash at any point loses at most the in-flight stage; on resume the
  engine restarts from ``current_stage`` with the context saved so far.
* Successful dependency results are stored in the context, so a resumed or
  retried request never calls a dependency again for a stage that completed.
  For the in-flight stage the dependency call is repeated with the same
  idempotency token, which the dependency deduplicates.
* Two workers racing on the same request cannot both commit a step: the
  loser's CAS fails with ``ConcurrencyConflict`` and it simply stops.

This mirrors the "durable execution" model used by Temporal (event history +
stored activity results) at a much smaller scale.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from ..audit import audit_log
from ..config.registry import WorkflowRegistry
from ..config.schema import TERMINALS, DependencyStage
from ..dependencies.simulated import DependencyHub
from ..domain.clock import Clock, parse_iso
from ..domain.models import TERMINAL_TARGET_TO_STATUS, RequestStatus, StageResult
from ..persistence import repository as repo
from ..persistence.db import Database
from ..resilience.policies import BackoffPolicy
from .stages import HANDLERS, StageContext


class DefinitionIntegrityError(RuntimeError):
    pass


MIN_LEASE_S = 30.0


def lease_seconds(wf: Any) -> float:
    """Idempotency lease long enough to outlive the slowest single stage (timeouts x attempts + backoff).
    The lease is also renewed at every checkpoint, so it only has to cover one stage, not a whole run."""
    return max(MIN_LEASE_S, wf.max_stage_seconds * 1.5 + 5)


def build_variables(context: dict[str, Any], params: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    variables = {k: v for k, v in context.items() if not k.startswith("_")}
    variables["params"] = params
    variables["meta"] = meta
    variables.setdefault("derived", {})
    return variables


class WorkflowEngine:
    def __init__(
        self,
        db: Database,
        registry: WorkflowRegistry,
        hub: DependencyHub,
        clock: Clock,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.registry = registry
        self.hub = hub
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()

    # Public
    def run(self, request_id: str, actor: str = "engine") -> repo.RequestRow:
        """Advance a request until it reaches a terminal or parked state."""
        row = repo.get_request(self.db.read(), request_id)

        if row.status in (RequestStatus.RECEIVED, RequestStatus.RETRY_PENDING):
            row, claimed = self._start(row, actor)
            if not claimed:
                return row  # another worker started it between our read and our claim
        elif row.status == RequestStatus.IN_PROGRESS:
            # Resuming a request left IN_PROGRESS (crash recovery or idempotency-lease takeover): claim it with a
            # CAS so two resumers cannot both proceed. A still-running original loses its next CAS and stops.
            row = self._claim(row, actor)
        if row.status != RequestStatus.IN_PROGRESS:
            return row

        wf = self.registry.get(row.workflow, row.workflow_version)
        if wf.content_hash != row.definition_hash:
            raise DefinitionIntegrityError(
                f"request {row.id} pinned definition {row.definition_hash[:12]} but registry holds "
                f"{wf.content_hash[:12]} for {row.workflow} v{row.workflow_version}"
            )
        stages = wf.config.stage_map()

        for _ in range(len(stages) + 1):  # DAG => at most |stages| steps
            stage = stages[row.current_stage]  # type: ignore[index]
            started = self.clock.iso()
            if isinstance(stage, DependencyStage):
                self._audit_call_intent(row, stage, actor)
            result = self._execute_stage(stage, row, wf)
            row = self._commit_step(row, wf, stage, result, started, actor)
            if row.status != RequestStatus.IN_PROGRESS:
                return row
        raise RuntimeError(f"request {row.id} exceeded step budget; workflow graph is not a DAG")  # pragma: no cover

    # Internals
    def _execute_stage(self, stage: Any, row: repo.RequestRow, wf: Any) -> StageResult:
        """Run a handler; an unexpected bug or adapter error becomes a committed, explained manual review
        instead of an exception that would leave the request stuck IN_PROGRESS."""
        try:
            return HANDLERS[stage.type](stage, self._stage_context(row, wf))
        except Exception as exc:
            return StageResult(
                "error",
                "manual_review",
                trace={"engine_error": f"{type(exc).__name__}: {exc}", "stage_type": stage.type},
                reasons=[f"Stage '{stage.id}' failed unexpectedly ({type(exc).__name__}); routed to manual review"],
            )

    def _stage_context(self, row: repo.RequestRow, wf: Any) -> StageContext:
        meta = {
            "request_id": row.id,
            "workflow": row.workflow,
            "version": row.workflow_version,
            "retry_count": row.retry_count,
            "evaluation_date": row.evaluation_date,
            "submitted_at": row.created_at,
        }
        return StageContext(
            request_id=row.id,
            workflow=wf,
            variables=build_variables(row.context, wf.config.parameters, meta),
            today=parse_iso(row.evaluation_date).date(),
            hub=self.hub,
            sleep=self.sleep,
            rng=self.rng,
        )

    def _claim(self, row: repo.RequestRow, actor: str) -> repo.RequestRow:
        now = self.clock.iso()
        with self.db.transaction() as tx:
            row.updated_at = now
            repo.update_request(tx, row, row.lock_version)  # raises ConcurrencyConflict if someone else moved it
            audit_log.append(
                tx,
                request_id=row.id,
                event_type="engine.resumed",
                actor=actor,
                at=now,
                payload={"stage": row.current_stage, "lock_version": row.lock_version},
            )
        return row

    def _start(self, row: repo.RequestRow, actor: str) -> tuple[repo.RequestRow, bool]:
        now = self.clock.iso()
        with self.db.transaction() as tx:
            fresh = repo.get_request(tx, row.id)
            if fresh.status not in (RequestStatus.RECEIVED, RequestStatus.RETRY_PENDING):
                return fresh, False  # someone else already picked it up
            src = fresh.status
            reason = "processing started" if src == RequestStatus.RECEIVED else f"retry attempt {fresh.retry_count}"
            fresh.status = RequestStatus.IN_PROGRESS
            fresh.next_retry_at = None
            fresh.updated_at = now
            repo.update_request(tx, fresh, fresh.lock_version)
            repo.record_transition(
                tx,
                request_id=fresh.id,
                src=src,
                dst=RequestStatus.IN_PROGRESS,
                stage_id=fresh.current_stage,
                reason=reason,
                actor=actor,
                at=now,
            )
            return fresh, True

    def _audit_call_intent(self, row: repo.RequestRow, stage: DependencyStage, actor: str) -> None:
        """Write-ahead record that an external call is about to happen (survives a crash mid-call)."""
        with self.db.transaction() as tx:
            audit_log.append(
                tx,
                request_id=row.id,
                event_type="dependency.call_started",
                actor=actor,
                at=self.clock.iso(),
                payload={
                    "stage": stage.id,
                    "dependency": stage.dependency,
                    "idempotency_token": f"{row.id}:{stage.id}",
                },
            )

    def _commit_step(
        self, row: repo.RequestRow, wf: Any, stage: Any, result: StageResult, started: str, actor: str
    ) -> repo.RequestRow:
        now = self.clock.iso()
        with self.db.transaction() as tx:
            current = repo.get_request(tx, row.id)
            if current.lock_version != row.lock_version or current.current_stage != stage.id:
                raise repo.ConcurrencyConflict(f"request {row.id} advanced by another worker")

            repo.insert_stage_run(
                tx,
                request_id=row.id,
                stage_id=stage.id,
                stage_type=stage.type,
                outcome=result.outcome,
                next_target=result.next_target,
                started_at=started,
                finished_at=now,
                trace=result.trace,
                context_patch=result.context_updates,
            )
            self.db.checkpoint("after_stage_run")

            ctx = dict(current.context)
            ctx.update(result.context_updates)
            trail = dict(ctx.get("_trail", {"path": [], "reasons": [], "flags": []}))
            trail["path"] = trail["path"] + [{"stage": stage.id, "outcome": result.outcome}]
            trail["reasons"] = trail["reasons"] + [{"stage": stage.id, "reason": r} for r in result.reasons]
            trail["flags"] = trail["flags"] + [{"stage": stage.id, "reason": r} for r in result.trace.get("flags", [])]
            ctx["_trail"] = trail
            current.context = ctx
            current.updated_at = now

            audit_log.append(
                tx,
                request_id=row.id,
                event_type="stage.completed",
                actor=actor,
                at=now,
                payload={
                    "stage": stage.id,
                    "type": stage.type,
                    "outcome": result.outcome,
                    "next": result.next_target,
                    "trace": result.trace,
                },
            )
            self.db.checkpoint("after_stage_audit")

            src = current.status
            if result.next_target is None:
                self._park_for_retry(tx, current, wf, stage.id, now, actor)
            elif result.next_target in TERMINALS:
                self._finish(tx, current, wf, stage.id, result.next_target, now, actor)
            else:
                current.current_stage = result.next_target

            repo.update_request(tx, current, row.lock_version)
            # Heartbeat: extend the idempotency lease while the request makes progress.
            tx.execute(
                "UPDATE idempotency_keys SET locked_until=? WHERE request_id=? AND state='processing'",
                (
                    (self.clock.now() + timedelta(seconds=lease_seconds(wf))).isoformat(timespec="milliseconds"),
                    current.id,
                ),
            )
            if current.status != src:
                reason = self._transition_reason(current, result, stage.id)
                repo.record_transition(
                    tx,
                    request_id=current.id,
                    src=src,
                    dst=current.status,
                    stage_id=stage.id,
                    reason=reason,
                    actor=actor,
                    at=now,
                )
                if current.status.is_terminal or current.status == RequestStatus.MANUAL_REVIEW:
                    self._emit_decision(tx, current, now, actor)
            self.db.checkpoint("after_request_update")
            return current

    @staticmethod
    def _transition_reason(row: repo.RequestRow, result: StageResult, stage_id: str) -> str:
        if row.status == RequestStatus.RETRY_PENDING:
            return f"dependency unavailable; retry {row.retry_count} scheduled for {row.next_retry_at}"
        if result.reasons:
            return "; ".join(result.reasons)
        return f"{row.status.value.lower()} at stage '{stage_id}' (outcome: {result.outcome})"

    def _park_for_retry(self, tx: Any, row: repo.RequestRow, wf: Any, stage_id: str, now: str, actor: str) -> None:
        cfg = wf.config.retry
        if row.retry_count >= cfg.max_request_retries:
            target = RequestStatus.FAILED if cfg.on_exhausted == "failed" else RequestStatus.MANUAL_REVIEW
            row.status = target
            row.next_retry_at = None
            row.decision = self._decision(
                row,
                wf,
                stage_id,
                target,
                actor="engine",
                extra_reason=f"retry budget exhausted after {row.retry_count} retries",
            )
            audit_log.append(
                tx,
                request_id=row.id,
                event_type="retry.exhausted",
                actor=actor,
                at=now,
                payload={"stage": stage_id, "retry_count": row.retry_count, "outcome": target.value},
            )
            return
        policy = BackoffPolicy(
            max_attempts=cfg.max_request_retries + 1, base_delay_s=cfg.base_delay_s, max_delay_s=cfg.max_delay_s
        )
        # Equal-ish jitter here: never schedule earlier than half the ceiling, avoids instant re-hammering.
        ceiling = policy.ceiling(row.retry_count)
        delay = ceiling / 2 + self.rng.uniform(0, ceiling / 2)
        row.retry_count += 1
        row.status = RequestStatus.RETRY_PENDING
        row.next_retry_at = (self.clock.now() + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
        audit_log.append(
            tx,
            request_id=row.id,
            event_type="retry.scheduled",
            actor=actor,
            at=now,
            payload={
                "stage": stage_id,
                "retry_count": row.retry_count,
                "next_retry_at": row.next_retry_at,
                "delay_s": round(delay, 3),
            },
        )

    def _finish(
        self, tx: Any, row: repo.RequestRow, wf: Any, stage_id: str, terminal: str, now: str, actor: str
    ) -> None:
        status = TERMINAL_TARGET_TO_STATUS[terminal]
        row.status = status
        row.current_stage = stage_id
        row.decision = self._decision(row, wf, stage_id, status, actor="engine")

    def _decision(
        self,
        row: repo.RequestRow,
        wf: Any,
        stage_id: str,
        status: RequestStatus,
        actor: str,
        extra_reason: str | None = None,
    ) -> dict[str, Any]:
        trail = row.context.get("_trail", {})
        reasons = [r["reason"] for r in trail.get("reasons", [])]
        if extra_reason:
            reasons.append(extra_reason)
        return {
            "outcome": status.value,
            "decided_by": actor,
            "decided_at": self.clock.iso(),
            "decided_at_stage": stage_id,
            "workflow": wf.name,
            "workflow_version": wf.version,
            "definition_hash": wf.content_hash,
            "reasons": reasons,
            "flags": [f["reason"] for f in trail.get("flags", [])],
            "path": [p["stage"] for p in trail.get("path", [])],
            "final": status != RequestStatus.MANUAL_REVIEW,
        }

    def _emit_decision(self, tx: Any, row: repo.RequestRow, now: str, actor: str) -> None:
        event = "review.requested" if row.status == RequestStatus.MANUAL_REVIEW else "decision.made"
        audit_log.append(tx, request_id=row.id, event_type=event, actor=actor, at=now, payload=row.decision or {})
        repo.enqueue_outbox(
            tx,
            request_id=row.id,
            event_type=event,
            payload={"request_id": row.id, "status": row.status.value, "decision": row.decision},
            at=now,
        )
