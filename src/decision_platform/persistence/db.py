"""SQLite persistence with explicit unit-of-work transactions.

Design notes
* WAL journal mode: readers do not block the writer and vice versa; SQLite
  still allows a single writer at a time, which we embrace (see SCALING.md).
* Every state change is one ``BEGIN IMMEDIATE`` transaction that writes the
  request row, the stage-run record, the transition history, the audit events
  and any outbox message together. Either all of it commits or none of it
  does, so there is no "partial save" a reader could ever observe.
* ``fault_injector`` lets tests raise inside a transaction at named points to
  prove the rollback behaviour.
* One connection per thread (FastAPI runs sync endpoints in a thread pool).
"""

from __future__ import annotations

import contextlib
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_definitions (
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    definition    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL,
    PRIMARY KEY (name, version)
);

CREATE TABLE IF NOT EXISTS workflow_activations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    version       INTEGER NOT NULL,
    activated_at  TEXT NOT NULL,
    activated_by  TEXT NOT NULL,
    reason        TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (name, version) REFERENCES workflow_definitions(name, version)
);

CREATE TABLE IF NOT EXISTS requests (
    id                TEXT PRIMARY KEY,
    workflow          TEXT NOT NULL,
    workflow_version  INTEGER NOT NULL,
    definition_hash   TEXT NOT NULL,
    status            TEXT NOT NULL,
    current_stage     TEXT,
    context           TEXT NOT NULL,
    business_key      TEXT,
    idempotency_key   TEXT,
    payload_hash      TEXT NOT NULL,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    next_retry_at     TEXT,
    decision          TEXT,
    evaluation_date   TEXT NOT NULL,
    lock_version      INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_requests_business ON requests(workflow, business_key, created_at);
CREATE INDEX IF NOT EXISTS ix_requests_retry ON requests(status, next_retry_at);
CREATE INDEX IF NOT EXISTS ix_requests_status ON requests(workflow, status);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    request_id    TEXT NOT NULL,
    state         TEXT NOT NULL,
    http_status   INTEGER,
    response      TEXT,
    locked_until  TEXT,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id     TEXT NOT NULL REFERENCES requests(id),
    stage_id       TEXT NOT NULL,
    stage_type     TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    next_target    TEXT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT NOT NULL,
    trace          TEXT NOT NULL,
    context_patch  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_stage_runs_request ON stage_runs(request_id, id);

CREATE TABLE IF NOT EXISTS transitions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL REFERENCES requests(id),
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    stage_id     TEXT,
    reason       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_transitions_request ON transitions(request_id, id);

CREATE TABLE IF NOT EXISTS audit_events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT,
    event_type   TEXT NOT NULL,
    actor        TEXT NOT NULL,
    at           TEXT NOT NULL,
    payload      TEXT NOT NULL,
    prev_hash    TEXT NOT NULL,
    hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_events(request_id, seq);

CREATE TABLE IF NOT EXISTS reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL REFERENCES requests(id),
    reviewer     TEXT NOT NULL,
    decision     TEXT NOT NULL,
    comment      TEXT NOT NULL,
    at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id    TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    published_at  TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox(published_at, id);
"""


class InjectedFault(RuntimeError):
    """Raised by the test fault injector to simulate a crash mid-transaction."""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self.fault_injector: Callable[[str], None] | None = None
        # executescript manages its own transaction (DDL is idempotent: CREATE ... IF NOT EXISTS).
        self._conn().executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Unit of work. BEGIN IMMEDIATE takes the write lock up-front to avoid lock-upgrade deadlocks."""
        conn = self._conn()
        if conn.in_transaction:  # nested use joins the outer transaction
            yield conn
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            self.checkpoint("before_commit")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def read(self) -> sqlite3.Connection:
        return self._conn()

    def checkpoint(self, name: str) -> None:
        """Named fault-injection point. No-op in production."""
        if self.fault_injector is not None:
            self.fault_injector(name)

    def close(self) -> None:
        """Close every connection opened by any thread."""
        with self._conns_lock:
            for conn in self._all_conns:
                with contextlib.suppress(sqlite3.ProgrammingError):
                    conn.close()
            self._all_conns.clear()
        self._local = threading.local()
