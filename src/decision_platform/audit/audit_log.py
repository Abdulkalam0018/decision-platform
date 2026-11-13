"""Append-only, hash-chained audit log.

Each event stores ``hash = SHA-256(prev_hash || canonical(event))``. Altering,
deleting or reordering any historical row breaks every hash after it, which
``verify_chain`` detects. This is the Kelsey-Schneier hash-chain
construction; Crosby and Wallach (USENIX Security 2009) show how to get
logarithmic audit cost with a Merkle "history tree" if the log becomes huge
(see docs/SCALING.md).

Events are written inside the same database transaction as the state change
they describe, so the audit trail can never disagree with the data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..config.compiler import canonical_json

GENESIS = "0" * 64


def _digest(prev_hash: str, request_id: str | None, event_type: str, actor: str, at: str, payload: str) -> str:
    material = canonical_json(
        {"prev": prev_hash, "request_id": request_id, "type": event_type, "actor": actor, "at": at, "payload": payload}
    )
    return hashlib.sha256(material.encode()).hexdigest()


def append(
    tx: sqlite3.Connection, *, request_id: str | None, event_type: str, actor: str, at: str, payload: dict[str, Any]
) -> int:
    row = tx.execute("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    prev = row["hash"] if row else GENESIS
    body = canonical_json(payload)
    h = _digest(prev, request_id, event_type, actor, at, body)
    cur = tx.execute(
        "INSERT INTO audit_events(request_id, event_type, actor, at, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
        (request_id, event_type, actor, at, body, prev, h),
    )
    return int(cur.lastrowid or 0)


@dataclass
class ChainVerification:
    ok: bool
    events_checked: int
    first_bad_seq: int | None = None
    problem: str | None = None
    head_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def verify_chain(conn: sqlite3.Connection) -> ChainVerification:
    prev = GENESIS
    count = 0
    for row in conn.execute("SELECT * FROM audit_events ORDER BY seq ASC"):
        count += 1
        if row["prev_hash"] != prev:
            return ChainVerification(
                False, count, row["seq"], "prev_hash does not match previous event (row deleted or reordered)"
            )
        expected = _digest(prev, row["request_id"], row["event_type"], row["actor"], row["at"], row["payload"])
        if expected != row["hash"]:
            return ChainVerification(False, count, row["seq"], "content hash mismatch (row modified)")
        prev = row["hash"]
    return ChainVerification(True, count, head_hash=prev)


def events_for(conn: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM audit_events WHERE request_id=? ORDER BY seq", (request_id,)).fetchall()
    return [
        {
            "seq": r["seq"],
            "event_type": r["event_type"],
            "actor": r["actor"],
            "at": r["at"],
            "payload": json.loads(r["payload"]),
            "hash": r["hash"],
        }
        for r in rows
    ]
