"""Compile and statically validate a workflow definition.

Validation happens once, before a definition can be activated. A definition
that fails any check is rejected with every problem listed, so an operator
never activates a half-broken workflow. Checks performed:

1. Schema: pydantic model (types, required fields, reserved ids).
2. Input schema: must itself be a valid JSON Schema (Draft 2020-12).
3. Graph: unique stage ids, every transition target exists, start exists,
   no cycles (so every execution terminates), no unreachable stages.
4. References: dependencies exist and their adapter type is registered;
   every ``params.x`` exists in ``parameters``; rule ids are unique.
5. Expressions: every expression compiles in the sandbox.
6. Data flow: a stage may only read ``derived.x`` or a dependency output
   namespace if that value is produced on *every* path leading to it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from ..rules.expression import (
    TEMPLATE_RE,
    CompiledExpression,
    ExpressionError,
    check_regex,
    compile_expression,
)
from .schema import (
    TERMINALS,
    BranchStage,
    ComputeStage,
    DependencyStage,
    RulesStage,
    WorkflowConfig,
)


class WorkflowValidationError(ValueError):
    def __init__(self, workflow: str, errors: list[str]):
        self.workflow = workflow
        self.errors = errors
        super().__init__(f"workflow '{workflow}' is invalid:\n  - " + "\n  - ".join(errors))


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def content_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode()).hexdigest()


@dataclass
class CompiledWorkflow:
    config: WorkflowConfig
    raw: dict[str, Any]
    content_hash: str
    expressions: dict[str, CompiledExpression] = field(default_factory=dict)
    input_validator: Draft202012Validator | None = None
    max_stage_seconds: float = 0.0  # worst-case duration of one stage (dependency timeouts + backoff)

    @property
    def name(self) -> str:
        return self.config.workflow

    @property
    def version(self) -> int:
        return self.config.version

    def expr(self, source: str) -> CompiledExpression:
        return self.expressions[source]


def _stage_expressions(stage: Any) -> list[tuple[str, str]]:
    """Return (label, expression) pairs for a stage."""
    out: list[tuple[str, str]] = []
    if isinstance(stage, RulesStage):
        for r in stage.rules:
            if r.when:
                out.append((f"{stage.id}.{r.id}.when", r.when))
            out.append((f"{stage.id}.{r.id}.condition", r.condition))
    elif isinstance(stage, ComputeStage):
        out.extend((f"{stage.id}.outputs.{k}", v) for k, v in stage.outputs.items())
    elif isinstance(stage, DependencyStage):
        out.extend((f"{stage.id}.request.{k}", v) for k, v in stage.request.items())
    elif isinstance(stage, BranchStage):
        out.extend((f"{stage.id}.branches[{i}].when", b.when) for i, b in enumerate(stage.branches))
    return out


def _produces(stage: Any) -> set[str]:
    if isinstance(stage, ComputeStage):
        return {f"derived.{k}" for k in stage.outputs}
    if isinstance(stage, DependencyStage):
        return {stage.output_key}
    return set()


def _symbol_for(var: str) -> str | None:
    """Map a variable reference to the symbol that must be available, or None if always available."""
    root = var.split(".")[0]
    if root in ("input", "params", "meta"):
        return None
    if root == "derived":
        parts = var.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else "derived"
    return root


def _remote_refs(node: Any, path: str = "$") -> list[str]:
    """Find $ref values that are not local JSON pointers (would trigger network/file resolution at request time)."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and not (isinstance(v, str) and v.startswith("#")):
                found.append(f"{path}.$ref={v!r}")
            found.extend(_remote_refs(v, f"{path}.{k}"))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found.extend(_remote_refs(v, f"{path}[{i}]"))
    return found


def _schema_has_path(schema: Any, dotted: str) -> bool:
    node = schema
    for part in dotted.split("."):
        props = node.get("properties") if isinstance(node, dict) else None
        if not isinstance(props, dict) or part not in props:
            return False
        node = props[part]
    return True


def _worst_case_stage_seconds(cfg: WorkflowConfig) -> float:
    worst = 0.0
    for s in cfg.stages:
        if isinstance(s, DependencyStage) and s.dependency in cfg.dependencies:
            d = cfg.dependencies[s.dependency]
            attempts = d.retry.max_attempts
            backoff = sum(min(d.retry.max_delay_ms, d.retry.base_delay_ms * 2**i) for i in range(attempts - 1))
            worst = max(worst, (attempts * d.timeout_ms + backoff) / 1000)
    return worst


def compile_workflow(raw: dict[str, Any], registered_dependency_types: Iterable[str] | None = None) -> CompiledWorkflow:
    name = str(raw.get("workflow", "<unnamed>")) if isinstance(raw, dict) else "<invalid>"
    try:
        cfg = WorkflowConfig.model_validate(raw)
    except ValidationError as exc:
        messages = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
        raise WorkflowValidationError(name, messages) from None

    errors: list[str] = []

    # 2. input schema
    validator: Draft202012Validator | None = None
    try:
        Draft202012Validator.check_schema(cfg.input_schema)
        validator = Draft202012Validator(cfg.input_schema)
    except SchemaError as exc:
        errors.append(f"input_schema is not a valid JSON Schema: {exc.message}")
    for ref in _remote_refs(cfg.input_schema):
        errors.append(f"input_schema uses a non-local reference {ref}; only '#/...' references are allowed")
    if cfg.duplicate_check:
        for f in cfg.duplicate_check.fields:
            if not _schema_has_path(cfg.input_schema, f):
                errors.append(f"duplicate_check field '{f}' is not declared in input_schema properties")

    # 3. graph structure
    stages = cfg.stage_map()
    if len(stages) != len(cfg.stages):
        seen: set[str] = set()
        dups = {s.id for s in cfg.stages if s.id in seen or seen.add(s.id)}  # type: ignore[func-returns-value]
        errors.append(f"duplicate stage ids: {sorted(dups)}")
    if cfg.start_stage not in stages:
        errors.append(f"start stage '{cfg.start_stage}' does not exist")

    edges: dict[str, set[str]] = {sid: set() for sid in stages}
    for s in cfg.stages:
        for label, target in s.targets().items():
            if target in TERMINALS:
                continue
            if target not in stages:
                errors.append(f"stage '{s.id}' transition '{label}' -> unknown target '{target}'")
            else:
                edges[s.id].add(target)

    predecessors: dict[str, set[str]] = {sid: set() for sid in stages}
    for src, dsts in edges.items():
        for d in dsts:
            predecessors[d].add(src)

    order: list[str] = []
    try:
        order = list(TopologicalSorter(predecessors).static_order())
    except CycleError as exc:
        errors.append(f"workflow graph contains a cycle: {' -> '.join(exc.args[1])}")

    if cfg.start_stage in stages:
        reachable, frontier = {cfg.start_stage}, [cfg.start_stage]
        while frontier:
            for nxt in edges[frontier.pop()]:
                if nxt not in reachable:
                    reachable.add(nxt)
                    frontier.append(nxt)
        unreachable = set(stages) - reachable
        if unreachable:
            errors.append(f"unreachable stages: {sorted(unreachable)}")

    # 4. references
    rule_ids: set[str] = set()
    known_types = set(registered_dependency_types) if registered_dependency_types is not None else None
    for dep_name, dep in cfg.dependencies.items():
        if known_types is not None and dep.type not in known_types:
            errors.append(f"dependency '{dep_name}' uses unregistered adapter type '{dep.type}'")
    output_keys: set[str] = set()
    for s in cfg.stages:
        if isinstance(s, RulesStage):
            for r in s.rules:
                if r.id in rule_ids:
                    errors.append(f"duplicate rule id '{r.id}'")
                rule_ids.add(r.id)
        if isinstance(s, DependencyStage):
            if s.dependency not in cfg.dependencies:
                errors.append(f"stage '{s.id}' references undeclared dependency '{s.dependency}'")
            if s.output_key in output_keys:
                errors.append(f"output_key '{s.output_key}' is produced by more than one stage")
            output_keys.add(s.output_key)

    # 5. expressions
    allowed_roots = {"input", "params", "derived", "meta"} | output_keys
    compiled: dict[str, CompiledExpression] = {}
    stage_vars: dict[str, set[str]] = {sid: set() for sid in stages}
    for s in cfg.stages:
        for label, src in _stage_expressions(s):
            try:
                ce = compile_expression(src, allowed_roots)
            except ExpressionError as exc:
                errors.append(f"{label}: {exc}")
                continue
            compiled[src] = ce
            stage_vars[s.id] |= set(ce.variables)
            for var in ce.variables:
                if var.startswith("params."):
                    key = var.split(".")[1]
                    if key not in cfg.parameters:
                        errors.append(f"{label}: unknown parameter '{key}'")
            for key in ce.regex_params:
                if key in cfg.parameters:
                    try:
                        check_regex(cfg.parameters[key])
                    except ExpressionError as exc:
                        errors.append(f"{label}: parameter '{key}' used as regex: {exc}")
        # reason templates are shown to customers and auditors: validate their placeholders too
        if isinstance(s, RulesStage):
            for r in s.rules:
                for var in TEMPLATE_RE.findall(r.reason):
                    root = var.split(".")[0]
                    if root not in allowed_roots:
                        errors.append(f"{s.id}.{r.id}.reason: unknown placeholder root '{root}' in '{{{var}}}'")
                    elif root == "params" and var.split(".")[1:2] and var.split(".")[1] not in cfg.parameters:
                        errors.append(f"{s.id}.{r.id}.reason: unknown parameter in placeholder '{{{var}}}'")
                    else:
                        stage_vars[s.id].add(var)

    # 6. data-flow: symbols available on every path (DAG, so topological order works)
    if order and not any("cycle" in e for e in errors):
        available: dict[str, set[str]] = {}
        for sid in order:
            preds = predecessors[sid]
            if not preds:
                avail_in: set[str] = set()
            else:
                sets = [available[p] | _produces(stages[p]) for p in preds]
                avail_in = set.intersection(*sets)
            available[sid] = avail_in
            for var in sorted(stage_vars[sid]):
                sym = _symbol_for(var)
                if sym is None:
                    continue
                # A compute stage can read its own earlier outputs is NOT allowed (order is undefined).
                if sym not in avail_in:
                    errors.append(f"stage '{sid}' reads '{var}' but '{sym}' is not produced on every path before it")

    if errors:
        raise WorkflowValidationError(cfg.workflow, errors)

    return CompiledWorkflow(
        config=cfg,
        raw=raw,
        content_hash=content_hash(raw),
        expressions=compiled,
        input_validator=validator,
        max_stage_seconds=_worst_case_stage_seconds(cfg),
    )
