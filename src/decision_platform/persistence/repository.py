"""Row-level data access. All functions take an open connection/transaction."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..audit import audit_log
from ..config.compiler import canonical_json
from ..domain.models import RequestStatus, check_transition


class ConcurrencyConflict(RuntimeError):
    """Another worker advanced this request first (optimistic lock lost)."""


class NotFound(KeyError):
    pass


@dataclass
class RequestRow:
    id: str
    workflow: str
    workflow_version: int
    definition_hash: str
    status: RequestStatus
    current_stage: str | None
    context: dict[str, Any]
    business_key: str | None
    idempotency_key: str | None
    payload_hash: str
    retry_count: int
    next_retry_at: str | None
    decision: dict[str, Any] | None
    evaluation_date: str
    lock_version: int
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> RequestRow:
        return cls(
            id=r["id"],
            workflow=r["workflow"],
            workflow_version=r["workflow_version"],
            definition_hash=r["definition_hash"],
            status=RequestStatus(r["status"]),
            current_stage=r["current_stage"],
            context=json.loads(r["context"]),
            business_key=r["business_key"],
            idempotency_key=r["idempotency_key"],
            payload_hash=r["payload_hash"],
            retry_count=r["retry_count"],
            next_retry_at=r["next_retry_at"],
            decision=json.loads(r["decision"]) if r["decision"] else None,
            evaluation_date=r["evaluation_date"],
            lock_version=r["lock_version"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def view(self) -> dict[str, Any]:
        return {
            "request_id": self.id,
            "workflow": self.workflow,
            "workflow_version": self.workflow_version,
            "status": self.status.value,
            "current_stage": self.current_stage,
            "decision": self.decision,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def get_request(conn: sqlite3.Connection, request_id: str) -> RequestRow:
    r = conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    if not r:
        raise NotFound(f"request '{request_id}' not found")
    return RequestRow.from_row(r)


def insert_request(tx: sqlite3.Connection, row: RequestRow) -> None:
    tx.execute(
        "INSERT INTO requests(id, workflow, workflow_version, definition_hash, status, current_stage, context, "
        "business_key, idempotency_key, payload_hash, retry_count, next_retry_at, decision, evaluation_date, "
        "lock_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row.id,
            row.workflow,
            row.workflow_version,
            row.definition_hash,
            row.status.value,
            row.current_stage,
            canonical_json(row.context),
            row.business_key,
            row.idempotency_key,
            row.payload_hash,
            row.retry_count,
            row.next_retry_at,
            canonical_json(row.decision) if row.decision else None,
            row.evaluation_date,
            row.lock_version,
            row.created_at,
            row.updated_at,
        ),
    )


def update_request(tx: sqlite3.Connection, row: RequestRow, expected_lock: int) -> None:
    """Compare-and-swap update on lock_version."""
    cur = tx.execute(
        "UPDATE requests SET status=?, current_stage=?, context=?, retry_count=?, next_retry_at=?, decision=?, "
        "lock_version=?, updated_at=? WHERE id=? AND lock_version=?",
        (
            row.status.value,
            row.current_stage,
            canonical_json(row.context),
            row.retry_count,
            row.next_retry_at,
            canonical_json(row.decision) if row.decision else None,
            expected_lock + 1,
            row.updated_at,
            row.id,
            expected_lock,
        ),
    )
    if cur.rowcount != 1:
        raise ConcurrencyConflict(f"request {row.id} was modified concurrently")
    row.lock_version = expected_lock + 1


def record_transition(
    tx: sqlite3.Connection,
    *,
    request_id: str,
    src: RequestStatus | None,
    dst: RequestStatus,
    stage_id: str | None,
    reason: str,
    actor: str,
    at: str,
) -> None:
    if src is not None:
        check_transition(src, dst)
    tx.execute(
        "INSERT INTO transitions(request_id, from_status, to_status, stage_id, reason, actor, at) "
        "VALUES (?,?,?,?,?,?,?)",
        (request_id, src.value if src else None, dst.value, stage_id, reason, actor, at),
    )
    audit_log.append(
        tx,
        request_id=request_id,
        event_type="state.changed",
        actor=actor,
        at=at,
        payload={"from": src.value if src else None, "to": dst.value, "stage": stage_id, "reason": reason},
    )


def insert_stage_run(
    tx: sqlite3.Connection,
    *,
    request_id: str,
    stage_id: str,
    stage_type: str,
    outcome: str,
    next_target: str | None,
    started_at: str,
    finished_at: str,
    trace: dict[str, Any],
    context_patch: dict[str, Any],
) -> int:
    cur = tx.execute(
        "INSERT INTO stage_runs(request_id, stage_id, stage_type, outcome, next_target, started_at, finished_at, "
        "trace, context_patch) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            request_id,
            stage_id,
            stage_type,
            outcome,
            next_target,
            started_at,
            finished_at,
            canonical_json(trace),
            canonical_json(context_patch),
        ),
    )
    return int(cur.lastrowid or 0)


def stage_runs(conn: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM stage_runs WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
    return [
        {
            "id": r["id"],
            "stage_id": r["stage_id"],
            "stage_type": r["stage_type"],
            "outcome": r["outcome"],
            "next_target": r["next_target"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "trace": json.loads(r["trace"]),
            "context_patch": json.loads(r["context_patch"]),
        }
        for r in rows
    ]


def transitions(conn: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT from_status, to_status, stage_id, reason, actor, at FROM transitions "
            "WHERE request_id=? ORDER BY id",
            (request_id,),
        )
    ]


def enqueue_outbox(
    tx: sqlite3.Connection, *, request_id: str, event_type: str, payload: dict[str, Any], at: str
) -> None:
    tx.execute(
        "INSERT INTO outbox(request_id, event_type, payload, created_at) VALUES (?,?,?,?)",
        (request_id, event_type, canonical_json(payload), at),
    )
