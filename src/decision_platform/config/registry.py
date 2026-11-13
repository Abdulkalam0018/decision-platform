"""Versioned workflow registry.

* Definitions are immutable per (name, version). Re-registering identical
  content is a no-op; different content under an existing version is refused,
  so a version number always means exactly one rule set.
* Activation is a separate, audited step. The active version is the latest
  activation row, which makes rollback just "activate the previous version".
* Requests pin the version (and content hash) they started with, so a rule
  change never alters an in-flight or historical decision.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from ..audit import audit_log
from ..persistence.db import Database
from .compiler import CompiledWorkflow, WorkflowValidationError, canonical_json, compile_workflow


class VersionConflictError(ValueError):
    pass


class UnknownWorkflowError(KeyError):
    pass


def parse_definition(text: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowValidationError("<unparsed>", [f"definition is not valid YAML/JSON: {exc}"]) from None
    if not isinstance(data, dict):
        raise WorkflowValidationError("<unparsed>", ["definition must be a mapping (YAML or JSON object)"])
    return data


class WorkflowRegistry:
    def __init__(self, db: Database, dependency_types: Iterable[str], now: Callable[[], str]):
        self.db = db
        self.dependency_types = list(dependency_types)
        self._now = now
        self._cache: dict[tuple[str, int], CompiledWorkflow] = {}
        self._lock = threading.Lock()

    # Write side
    def register(
        self, raw: dict[str, Any], *, actor: str, activate: bool = False, reason: str = ""
    ) -> CompiledWorkflow:
        try:
            compiled = compile_workflow(raw, self.dependency_types)
        except WorkflowValidationError as exc:
            with self.db.transaction() as tx:
                audit_log.append(
                    tx,
                    request_id=None,
                    event_type="config.rejected",
                    actor=actor,
                    at=self._now(),
                    payload={
                        "workflow": exc.workflow,
                        "version": raw.get("version") if isinstance(raw, dict) else None,
                        "errors": exc.errors,
                    },
                )
            raise
        with self.db.transaction() as tx:
            row = tx.execute(
                "SELECT content_hash FROM workflow_definitions WHERE name=? AND version=?",
                (compiled.name, compiled.version),
            ).fetchone()
            if row and row["content_hash"] != compiled.content_hash:
                raise VersionConflictError(
                    f"{compiled.name} v{compiled.version} already exists with different content; "
                    "versions are immutable, publish a new version number instead"
                )
            if not row:
                tx.execute(
                    "INSERT INTO workflow_definitions(name, version, content_hash, definition, created_at, created_by) "
                    "VALUES (?,?,?,?,?,?)",
                    (compiled.name, compiled.version, compiled.content_hash, canonical_json(raw), self._now(), actor),
                )
                audit_log.append(
                    tx,
                    request_id=None,
                    event_type="config.registered",
                    actor=actor,
                    at=self._now(),
                    payload={
                        "workflow": compiled.name,
                        "version": compiled.version,
                        "content_hash": compiled.content_hash,
                    },
                )
            if activate:
                self._activate_tx(tx, compiled.name, compiled.version, actor, reason)
        with self._lock:
            self._cache[(compiled.name, compiled.version)] = compiled
        return compiled

    def activate(self, name: str, version: int, *, actor: str, reason: str = "") -> CompiledWorkflow:
        compiled = self.get(name, version)
        with self.db.transaction() as tx:
            self._activate_tx(tx, name, version, actor, reason)
        return compiled

    def _activate_tx(self, tx: Any, name: str, version: int, actor: str, reason: str) -> None:
        current = tx.execute(
            "SELECT version FROM workflow_activations WHERE name=? ORDER BY id DESC LIMIT 1", (name,)
        ).fetchone()
        if current and current["version"] == version:
            return
        tx.execute(
            "INSERT INTO workflow_activations(name, version, activated_at, activated_by, reason) VALUES (?,?,?,?,?)",
            (name, version, self._now(), actor, reason),
        )
        audit_log.append(
            tx,
            request_id=None,
            event_type="config.activated",
            actor=actor,
            at=self._now(),
            payload={
                "workflow": name,
                "version": version,
                "previous_version": current["version"] if current else None,
                "reason": reason,
            },
        )

    def load_directory(self, directory: str | Path, *, actor: str = "bootstrap") -> list[CompiledWorkflow]:
        """Register every *.yaml/*.json file; activate the highest version of workflows that have no activation yet."""
        loaded: list[CompiledWorkflow] = []
        for path in sorted(Path(directory).glob("*.y*ml")) + sorted(Path(directory).glob("*.json")):
            loaded.append(self.register(parse_definition(path.read_text()), actor=actor))
        conn = self.db.read()
        for name in sorted({c.name for c in loaded}):
            has_active = conn.execute("SELECT 1 FROM workflow_activations WHERE name=?", (name,)).fetchone()
            if not has_active:
                top = max(c.version for c in loaded if c.name == name)
                self.activate(name, top, actor=actor, reason="initial load")
        return loaded

    # Read side
    def get(self, name: str, version: int) -> CompiledWorkflow:
        key = (name, version)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        row = (
            self.db.read()
            .execute("SELECT definition FROM workflow_definitions WHERE name=? AND version=?", key)
            .fetchone()
        )
        if not row:
            raise UnknownWorkflowError(f"workflow '{name}' version {version} not found")
        compiled = compile_workflow(json.loads(row["definition"]), self.dependency_types)
        with self._lock:
            self._cache[key] = compiled
        return compiled

    def active_version(self, name: str) -> int:
        row = (
            self.db.read()
            .execute("SELECT version FROM workflow_activations WHERE name=? ORDER BY id DESC LIMIT 1", (name,))
            .fetchone()
        )
        if not row:
            raise UnknownWorkflowError(f"workflow '{name}' has no active version")
        return int(row["version"])

    def active(self, name: str) -> CompiledWorkflow:
        return self.get(name, self.active_version(name))

    def list_versions(self) -> list[dict[str, Any]]:
        conn = self.db.read()
        out = []
        for r in conn.execute(
            "SELECT name, version, content_hash, created_at, created_by "
            "FROM workflow_definitions ORDER BY name, version"
        ):
            out.append(dict(r))
        actives = {
            r["name"]: r["version"]
            for r in conn.execute(
                "SELECT a.name, a.version FROM workflow_activations a "
                "JOIN (SELECT name, MAX(id) mid FROM workflow_activations GROUP BY name) m ON a.id = m.mid"
            )
        }
        for d in out:
            d["active"] = actives.get(d["name"]) == d["version"]
        return out

    def activation_history(self, name: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.read().execute(
                "SELECT version, activated_at, activated_by, reason FROM workflow_activations WHERE name=? ORDER BY id",
                (name,),
            )
        ]
