"""Application service: the single entry point used by both the REST API and the CLI.

Responsibilities (and nothing else): intake validation, idempotency,
business duplicate detection, invoking the engine, manual review, retries,
outbox relay, dry runs and impact analysis. Transport concerns (HTTP codes,
headers) live in the API layer; workflow semantics live in config + engine.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .audit import audit_log
from .config.compiler import CompiledWorkflow, canonical_json, content_hash
from .config.registry import UnknownWorkflowError, WorkflowRegistry
from .dependencies.simulated import DependencyHub
from .domain.clock import Clock, parse_iso
from .domain.models import RequestStatus
from .explain.explainer import build_explanation
from .persistence import repository as repo
from .persistence.db import Database
from .resilience.policies import DependencyError
from .workflow.engine import WorkflowEngine, lease_seconds
from .workflow.simulator import simulate

IDEMPOTENCY_TTL = timedelta(hours=24)
IDEMPOTENCY_LEASE = timedelta(seconds=30)
STALE_IN_PROGRESS = timedelta(minutes=5)


# Errors (mapped to HTTP in api/app.py)
class ServiceError(Exception):
    code = "error"

    def __init__(self, message: str, **details: Any):
        super().__init__(message)
        self.details = details


class InputValidationError(ServiceError):
    code = "invalid_input"


class IdempotencyKeyMissing(ServiceError):
    code = "idempotency_key_missing"


class IdempotencyKeyReuse(ServiceError):
    code = "idempotency_key_reused"


class RequestInProgress(ServiceError):
    code = "request_in_progress"


class DuplicateRequest(ServiceError):
    code = "duplicate_request"


class InvalidState(ServiceError):
    code = "invalid_state"


@dataclass
class SubmitResult:
    http_status: int
    body: dict[str, Any]
    replayed: bool = False
    duplicate_of: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _get_path(data: Any, path: str) -> Any:
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _normalise(value: Any) -> Any:
    """Make semantically equal payloads fingerprint identically (600000 == 600000.0, key order irrelevant)."""
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalise(v) for v in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def fingerprint(payload: Any) -> str:
    return content_hash(_normalise(payload))


def _schema_errors(wf: CompiledWorkflow, payload: Any) -> list[dict[str, str]]:
    assert wf.input_validator is not None
    errs = sorted(wf.input_validator.iter_errors(payload), key=lambda e: list(map(str, e.absolute_path)))
    return [
        {"field": ".".join(str(p) for p in e.absolute_path) or "$", "message": e.message, "rule": str(e.validator)}
        for e in errs
    ]


class DecisionService:
    def __init__(
        self,
        db: Database,
        *,
        clock: Clock | None = None,
        hub: DependencyHub | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.clock = clock or Clock()
        self.hub = hub or DependencyHub()
        self.registry = WorkflowRegistry(db, self.hub.types, self.clock.iso)
        self.engine = WorkflowEngine(db, self.registry, self.hub, self.clock, sleep=sleep, rng=rng)

    # Bootstrap
    def load_workflows(self, directory: str | Path) -> list[CompiledWorkflow]:
        return self.registry.load_directory(directory)

    # Intake
    def submit(self, workflow: str, payload: Any, idempotency_key: str | None, actor: str = "api") -> SubmitResult:
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 255:
            raise IdempotencyKeyMissing("Idempotency-Key header is required (1-255 characters) for request submission")
        wf = self.registry.active(workflow)  # raises UnknownWorkflowError

        errors = _schema_errors(wf, payload)
        if errors:
            with self.db.transaction() as tx:
                audit_log.append(
                    tx,
                    request_id=None,
                    event_type="intake.rejected",
                    actor=actor,
                    at=self.clock.iso(),
                    payload={
                        "workflow": workflow,
                        "version": wf.version,
                        "idempotency_key": idempotency_key,
                        "payload_sha256": fingerprint(payload),
                        # field + violated keyword only: messages can echo personal data
                        "errors": [{"field": e["field"], "rule": e["rule"]} for e in errors],
                    },
                )
            raise InputValidationError("request does not match the workflow input schema", errors=errors)

        fp = fingerprint(payload)
        lease = timedelta(seconds=lease_seconds(wf))
        now_dt = self.clock.now()
        now = now_dt.isoformat(timespec="milliseconds")
        resume_id: str | None = None

        with self.db.transaction() as tx:
            idem = tx.execute(
                "SELECT * FROM idempotency_keys WHERE scope=? AND key=?", (workflow, idempotency_key)
            ).fetchone()
            if idem and parse_iso(idem["expires_at"]) <= now_dt:
                tx.execute("DELETE FROM idempotency_keys WHERE scope=? AND key=?", (workflow, idempotency_key))
                idem = None
            if idem:
                if idem["fingerprint"] != fp:
                    audit_log.append(
                        tx,
                        request_id=idem["request_id"],
                        event_type="idempotency.conflict",
                        actor=actor,
                        at=now,
                        payload={"idempotency_key": idempotency_key, "payload_sha256": fp},
                    )
                    raise IdempotencyKeyReuse(
                        "Idempotency-Key was already used with a different payload",
                        request_id=idem["request_id"],
                    )
                if idem["state"] == "completed":
                    audit_log.append(
                        tx,
                        request_id=idem["request_id"],
                        event_type="idempotency.replayed",
                        actor=actor,
                        at=now,
                        payload={"idempotency_key": idempotency_key},
                    )
                    view = repo.get_request(tx, idem["request_id"]).view()
                    return SubmitResult(200, view, replayed=True, headers={"Idempotent-Replayed": "true"})
                if idem["locked_until"] and parse_iso(idem["locked_until"]) > now_dt:
                    raise RequestInProgress(
                        "a request with this Idempotency-Key is still being processed", request_id=idem["request_id"]
                    )
                # Lease expired: the original worker died. Take over and resume from the checkpoint.
                resume_id = idem["request_id"]
                tx.execute(
                    "UPDATE idempotency_keys SET locked_until=? WHERE scope=? AND key=?",
                    ((now_dt + lease).isoformat(timespec="milliseconds"), workflow, idempotency_key),
                )
                audit_log.append(
                    tx,
                    request_id=resume_id,
                    event_type="idempotency.lease_takeover",
                    actor=actor,
                    at=now,
                    payload={"idempotency_key": idempotency_key},
                )

            if resume_id is None:
                business_key = self._business_key(wf, payload)
                if business_key and wf.config.duplicate_check:
                    cutoff = (now_dt - timedelta(hours=wf.config.duplicate_check.window_hours)).isoformat(
                        timespec="milliseconds"
                    )
                    dup = tx.execute(
                        "SELECT id FROM requests WHERE workflow=? AND business_key=? AND created_at>=? "
                        # FAILED is a technical failure; the same request may be submitted again
                        "AND status != 'FAILED' ORDER BY created_at DESC LIMIT 1",
                        (workflow, business_key, cutoff),
                    ).fetchone()
                    if dup:
                        audit_log.append(
                            tx,
                            request_id=dup["id"],
                            event_type="duplicate.detected",
                            actor=actor,
                            at=now,
                            payload={
                                "idempotency_key": idempotency_key,
                                "business_key": business_key,
                                "action": wf.config.duplicate_check.action,
                            },
                        )
                        if wf.config.duplicate_check.action == "reject":
                            raise DuplicateRequest(
                                "an equivalent request was already submitted in the duplicate window",
                                existing_request_id=dup["id"],
                            )
                        self._insert_idem(tx, workflow, idempotency_key, fp, dup["id"], "completed", now_dt, 200, lease)
                        view = repo.get_request(tx, dup["id"]).view()
                        return SubmitResult(
                            200,
                            view,
                            replayed=True,
                            duplicate_of=dup["id"],
                            headers={"Idempotent-Replayed": "true", "Duplicate-Of": dup["id"]},
                        )

                request_id = str(uuid.uuid4())
                row = repo.RequestRow(
                    id=request_id,
                    workflow=workflow,
                    workflow_version=wf.version,
                    definition_hash=wf.content_hash,
                    status=RequestStatus.RECEIVED,
                    current_stage=wf.config.start_stage,
                    context={"input": payload},
                    business_key=business_key,
                    idempotency_key=idempotency_key,
                    payload_hash=fp,
                    retry_count=0,
                    next_retry_at=None,
                    decision=None,
                    evaluation_date=now,
                    lock_version=0,
                    created_at=now,
                    updated_at=now,
                )
                repo.insert_request(tx, row)
                self._insert_idem(tx, workflow, idempotency_key, fp, request_id, "processing", now_dt, None, lease)
                audit_log.append(
                    tx,
                    request_id=request_id,
                    event_type="request.received",
                    actor=actor,
                    at=now,
                    payload={
                        "workflow": workflow,
                        "version": wf.version,
                        "definition_hash": wf.content_hash,
                        "idempotency_key": idempotency_key,
                        "payload_sha256": fp,
                        "business_key": business_key,
                    },
                )
                repo.record_transition(
                    tx,
                    request_id=request_id,
                    src=None,
                    dst=RequestStatus.RECEIVED,
                    stage_id=None,
                    reason="input validated and persisted",
                    actor=actor,
                    at=now,
                )
                resume_id = request_id
                http_status = 201
            else:
                http_status = 200

        final = self._run_safely(resume_id, actor)
        if final.status not in (RequestStatus.RECEIVED, RequestStatus.IN_PROGRESS):
            # Only a settled request completes the key. If another worker still owns the request, the key keeps
            # its lease so a later retry returns the real outcome instead of a frozen IN_PROGRESS snapshot.
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE idempotency_keys SET state='completed', http_status=?, response=?, locked_until=NULL "
                    "WHERE scope=? AND key=?",
                    (http_status, canonical_json(final.view()), workflow, idempotency_key),
                )
        else:
            http_status = 202
        return SubmitResult(http_status, final.view(), headers={"Location": f"/v1/requests/{final.id}"})

    def _insert_idem(
        self,
        tx: Any,
        scope: str,
        key: str,
        fp: str,
        request_id: str,
        state: str,
        now_dt: Any,
        http_status: int | None,
        lease: timedelta = IDEMPOTENCY_LEASE,
    ) -> None:
        tx.execute(
            "INSERT INTO idempotency_keys(scope, key, fingerprint, request_id, state, http_status, locked_until, "
            "created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                scope,
                key,
                fp,
                request_id,
                state,
                http_status,
                (now_dt + lease).isoformat(timespec="milliseconds") if state == "processing" else None,
                now_dt.isoformat(timespec="milliseconds"),
                (now_dt + IDEMPOTENCY_TTL).isoformat(timespec="milliseconds"),
            ),
        )

    @staticmethod
    def _business_key(wf: CompiledWorkflow, payload: Any) -> str | None:
        dc = wf.config.duplicate_check
        if not dc:
            return None
        values = [_get_path(payload, f) for f in dc.fields]
        if any(v is None for v in values):
            return None
        # Hashed so personal identifiers (e.g. national id) are not stored in clear in an index.
        return hashlib.sha256(canonical_json(dict(zip(dc.fields, values, strict=True))).encode()).hexdigest()[:32]

    def _run_safely(self, request_id: str, actor: str) -> repo.RequestRow:
        try:
            return self.engine.run(request_id, actor="engine")
        except repo.ConcurrencyConflict:
            return repo.get_request(self.db.read(), request_id)

    # Queries
    def get(self, request_id: str) -> dict[str, Any]:
        return repo.get_request(self.db.read(), request_id).view()

    def list_requests(
        self, workflow: str | None = None, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM requests WHERE 1=1"
        args: list[Any] = []
        if workflow:
            sql += " AND workflow=?"
            args.append(workflow)
        if status:
            sql += " AND status=?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [repo.RequestRow.from_row(r).view() for r in self.db.read().execute(sql, args)]

    def history(self, request_id: str) -> dict[str, Any]:
        conn = self.db.read()
        row = repo.get_request(conn, request_id)
        return {
            "request": row.view(),
            "transitions": repo.transitions(conn, request_id),
            "stage_runs": repo.stage_runs(conn, request_id),
            "reviews": self._reviews(request_id),
        }

    def audit(self, request_id: str) -> list[dict[str, Any]]:
        repo.get_request(self.db.read(), request_id)
        return audit_log.events_for(self.db.read(), request_id)

    def explain(self, request_id: str) -> dict[str, Any]:
        conn = self.db.read()
        row = repo.get_request(conn, request_id)
        return build_explanation(
            row, repo.stage_runs(conn, request_id), repo.transitions(conn, request_id), self._reviews(request_id)
        )

    def _reviews(self, request_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.read().execute(
                "SELECT reviewer, decision, comment, at FROM reviews WHERE request_id=? ORDER BY id", (request_id,)
            )
        ]

    def verify_audit(self) -> dict[str, Any]:
        """Verify the hash chain AND that every stored decision/status matches what the chain says happened.

        The chain alone only protects audit rows; this cross-check detects a forged outcome written directly into
        the ``requests`` table. Rewriting the whole chain consistently is detectable only by anchoring
        ``head_hash`` outside the database (see SCALING.md)."""
        conn = self.db.read()
        chain = audit_log.verify_chain(conn).to_dict()
        mismatches: list[dict[str, Any]] = []
        rows = conn.execute("SELECT id, status, decision FROM requests").fetchall()
        for r in rows:
            last_state = conn.execute(
                "SELECT payload FROM audit_events WHERE request_id=? AND event_type='state.changed' "
                "ORDER BY seq DESC LIMIT 1",
                (r["id"],),
            ).fetchone()
            if not last_state or json.loads(last_state["payload"])["to"] != r["status"]:
                mismatches.append({"request_id": r["id"], "problem": "status differs from last audited transition"})
                continue
            if r["decision"]:
                last_dec = conn.execute(
                    "SELECT payload FROM audit_events WHERE request_id=? AND event_type IN "
                    "('decision.made','review.requested') ORDER BY seq DESC LIMIT 1",
                    (r["id"],),
                ).fetchone()
                if not last_dec or canonical_json(json.loads(last_dec["payload"])) != canonical_json(
                    json.loads(r["decision"])
                ):
                    mismatches.append({"request_id": r["id"], "problem": "decision differs from last audited decision"})
        chain["requests_checked"] = len(rows)
        chain["state_mismatches"] = mismatches
        chain["ok"] = chain["ok"] and not mismatches
        return chain

    # Retries
    def retry(self, request_id: str, actor: str = "operator") -> dict[str, Any]:
        row = repo.get_request(self.db.read(), request_id)
        if row.status != RequestStatus.RETRY_PENDING:
            raise InvalidState(
                f"request is {row.status.value}; only RETRY_PENDING requests can be retried",
                request_status=row.status.value,
            )
        with self.db.transaction() as tx:
            audit_log.append(
                tx,
                request_id=request_id,
                event_type="retry.triggered",
                actor=actor,
                at=self.clock.iso(),
                payload={"retry_count": row.retry_count, "scheduled_for": row.next_retry_at, "manual": True},
            )
        return self._run_safely(request_id, actor).view()

    def process_due_retries(self, limit: int = 100) -> dict[str, Any]:
        """Worker tick: resume due RETRY_PENDING requests and recover stale in-flight ones."""
        now_dt = self.clock.now()
        now = now_dt.isoformat(timespec="milliseconds")
        stale = (now_dt - STALE_IN_PROGRESS).isoformat(timespec="milliseconds")
        rows = (
            self.db.read()
            .execute(
                "SELECT id, status FROM requests WHERE (status='RETRY_PENDING' AND next_retry_at<=?) "
                "OR (status IN ('RECEIVED','IN_PROGRESS') AND updated_at<=?) ORDER BY updated_at LIMIT ?",
                (now, stale, limit),
            )
            .fetchall()
        )
        results = []
        for r in rows:
            try:
                final = self.engine.run(r["id"], actor="retry-worker")
                results.append({"request_id": r["id"], "from": r["status"], "to": final.status.value})
            except repo.ConcurrencyConflict:
                results.append(
                    {"request_id": r["id"], "from": r["status"], "to": "skipped (claimed by another worker)"}
                )
            except Exception as exc:
                results.append(
                    {"request_id": r["id"], "from": r["status"], "to": "error", "error": f"{type(exc).__name__}: {exc}"}
                )
                try:
                    with self.db.transaction() as tx:
                        audit_log.append(
                            tx,
                            request_id=r["id"],
                            event_type="worker.error",
                            actor="retry-worker",
                            at=self.clock.iso(),
                            payload={"error": f"{type(exc).__name__}: {exc}"},
                        )
                except Exception:
                    pass
        return {"processed": len(results), "results": results}

    # Manual review
    def review(self, request_id: str, decision: str, reviewer: str, comment: str) -> dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise InputValidationError(
                "decision must be 'approve' or 'reject'",
                errors=[{"field": "decision", "message": "must be approve or reject"}],
            )
        if not reviewer or not comment or len(comment.strip()) < 3:
            raise InputValidationError(
                "reviewer and a justification comment are required",
                errors=[{"field": "comment", "message": "at least 3 characters"}],
            )
        now = self.clock.iso()
        with self.db.transaction() as tx:
            row = repo.get_request(tx, request_id)
            review = (row.decision or {}).get("review") or {}
            if row.status.is_terminal and review.get("reviewer") == reviewer and review.get("decision") == decision:
                return row.view()  # client retry of the same review: idempotent, no second effect
            if row.status != RequestStatus.MANUAL_REVIEW:
                raise InvalidState(
                    f"request is {row.status.value}; only MANUAL_REVIEW requests accept a review",
                    request_status=row.status.value,
                )
            expected = row.lock_version
            dst = RequestStatus.APPROVED if decision == "approve" else RequestStatus.REJECTED
            tx.execute(
                "INSERT INTO reviews(request_id, reviewer, decision, comment, at) VALUES (?,?,?,?,?)",
                (request_id, reviewer, decision, comment, now),
            )
            prior = row.decision or {}
            row.decision = {
                **prior,
                "outcome": dst.value,
                "decided_by": f"reviewer:{reviewer}",
                "decided_at": now,
                "engine_recommendation": prior.get("outcome", RequestStatus.MANUAL_REVIEW.value),
                "review": {"reviewer": reviewer, "decision": decision, "comment": comment, "at": now},
                "final": True,
            }
            row.status = dst
            row.updated_at = now
            repo.update_request(tx, row, expected)
            repo.record_transition(
                tx,
                request_id=request_id,
                src=RequestStatus.MANUAL_REVIEW,
                dst=dst,
                stage_id=row.current_stage,
                reason=f"manual review by {reviewer}: {comment}",
                actor=f"reviewer:{reviewer}",
                at=now,
            )
            audit_log.append(
                tx,
                request_id=request_id,
                event_type="review.decided",
                actor=f"reviewer:{reviewer}",
                at=now,
                payload={"decision": decision, "comment": comment},
            )
            audit_log.append(
                tx,
                request_id=request_id,
                event_type="decision.made",
                actor=f"reviewer:{reviewer}",
                at=now,
                payload=row.decision,
            )
            repo.enqueue_outbox(
                tx,
                request_id=request_id,
                event_type="decision.made",
                payload={"request_id": request_id, "status": dst.value, "decision": row.decision},
                at=now,
            )
        return row.view()

    # Outbox
    def relay_outbox(self, limit: int = 100) -> dict[str, Any]:
        """At-least-once delivery of decision notifications (transactional outbox pattern)."""
        notifier = self.hub.get("notifier")
        rows = (
            self.db.read()
            .execute("SELECT * FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?", (limit,))
            .fetchall()
        )
        published, failed = 0, 0
        for r in rows:
            try:
                notifier.invoke(json.loads(r["payload"]), f"outbox:{r['id']}", timeout_ms=1000)
            except DependencyError as exc:
                failed += 1
                with self.db.transaction() as tx:
                    tx.execute("UPDATE outbox SET attempts=attempts+1, last_error=? WHERE id=?", (str(exc), r["id"]))
                continue
            published += 1
            with self.db.transaction() as tx:
                tx.execute(
                    "UPDATE outbox SET attempts=attempts+1, published_at=?, last_error=NULL WHERE id=?",
                    (self.clock.iso(), r["id"]),
                )
        pending = self.db.read().execute("SELECT COUNT(*) c FROM outbox WHERE published_at IS NULL").fetchone()["c"]
        return {"published": published, "failed": failed, "pending": pending}

    # Dry run & impact analysis
    def simulate(
        self,
        workflow: str,
        payload: Any,
        version: int | None = None,
        recorded_outputs: dict[str, Any] | None = None,
        evaluation_date: str | None = None,
    ) -> dict[str, Any]:
        wf = self.registry.get(workflow, version) if version else self.registry.active(workflow)
        errors = _schema_errors(wf, payload)
        if errors:
            raise InputValidationError("request does not match the workflow input schema", errors=errors)
        try:
            today = date.fromisoformat(evaluation_date) if evaluation_date else self.clock.now().date()
        except ValueError as exc:
            raise InputValidationError(
                "evaluation_date must be an ISO date (YYYY-MM-DD)",
                errors=[{"field": "evaluation_date", "message": "invalid date", "rule": "format"}],
            ) from exc
        # Dry runs never touch production adapters: a fresh sandbox hub gives the same deterministic responses
        # without consuming fault plans, moving circuit breakers or counting side effects.
        res = simulate(
            wf,
            payload,
            today,
            recorded_outputs=recorded_outputs,
            hub=DependencyHub() if recorded_outputs is None else None,
            run_id=f"dryrun-{uuid.uuid4()}",
        )
        return {"workflow": wf.name, "version": wf.version, "dry_run": True, **res.to_dict()}

    def impact(self, workflow: str, from_version: int, to_version: int, limit: int = 500) -> dict[str, Any]:
        """Replay historical requests decided under ``from_version`` against ``to_version`` using their stored inputs
        and recorded dependency responses (no live calls), and report every decision that would change."""
        src = self.registry.get(workflow, from_version)
        dst = self.registry.get(workflow, to_version)
        output_keys = {s.output_key for s in src.config.stages if s.type == "dependency"}
        rows = (
            self.db.read()
            .execute(
                "SELECT * FROM requests WHERE workflow=? AND workflow_version=? AND decision IS NOT NULL "
                "ORDER BY created_at DESC LIMIT ?",
                (workflow, from_version, limit),
            )
            .fetchall()
        )
        changes: list[dict[str, Any]] = []
        by_transition: dict[str, int] = {}
        skipped = 0
        for r in rows:
            row = repo.RequestRow.from_row(r)
            before = (row.decision or {}).get("engine_recommendation") or (row.decision or {}).get("outcome")
            recorded = {k: row.context[k] for k in output_keys if k in row.context}
            payload = row.context["input"]
            if _schema_errors(dst, payload):
                skipped += 1
                continue
            sim = simulate(dst, payload, parse_iso(row.evaluation_date).date(), recorded_outputs=recorded)
            after = sim.outcome if not sim.needs_live_data else "NEEDS_LIVE_DATA"
            key = f"{before}->{after}"
            by_transition[key] = by_transition.get(key, 0) + 1
            if before != after:
                changes.append({"request_id": row.id, "before": before, "after": after, "new_reasons": sim.reasons})
        return {
            "workflow": workflow,
            "from_version": from_version,
            "to_version": to_version,
            "replayed": len(rows) - skipped,
            "skipped_incompatible_input": skipped,
            "changed": len(changes),
            "unchanged": len(rows) - skipped - len(changes),
            "by_transition": dict(sorted(by_transition.items())),
            "changes": changes,
        }


__all__ = [
    "DecisionService",
    "DuplicateRequest",
    "IdempotencyKeyMissing",
    "IdempotencyKeyReuse",
    "InputValidationError",
    "InvalidState",
    "RequestInProgress",
    "ServiceError",
    "SubmitResult",
    "UnknownWorkflowError",
]
