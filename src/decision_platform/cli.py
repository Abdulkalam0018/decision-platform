"""Command-line interface. Uses the same DecisionService as the REST API.

dp workflows list | validate FILE | publish FILE [--activate] | activate NAME VERSION
dp submit WORKFLOW FILE --key KEY
dp show ID | history ID | audit ID | explain ID
dp retry ID | review ID --approve/--reject --reviewer R --comment C
dp worker [--once] [--interval S]
dp simulate WORKFLOW FILE [--version N]
dp impact WORKFLOW --from N --to M
dp audit-verify
dp serve [--host H] [--port P]
"""

from __future__ import annotations

import functools
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import yaml

from .config.compiler import WorkflowValidationError, compile_workflow
from .config.registry import parse_definition
from .persistence.db import Database
from .service import DecisionService, ServiceError


def _svc(ctx: click.Context) -> DecisionService:
    if ctx.obj is None:
        db = Database(os.environ.get("DP_DB_PATH", "data/decisions.db"))
        svc = DecisionService(db)
        svc.load_workflows(os.environ.get("DP_CONFIG_DIR", "config/workflows"))
        ctx.obj = svc
        ctx.call_on_close(db.close)
    return ctx.obj


def _out(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _load(path: str) -> Any:
    text = Path(path).read_text() if path != "-" else sys.stdin.read()
    return yaml.safe_load(text)


@click.group()
@click.pass_context
def main(ctx: click.Context) -> None:
    """Configurable Workflow Decision Platform."""


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def wrapper(*a: Any, **kw: Any) -> Any:
        try:
            return fn(*a, **kw)
        except ServiceError as exc:
            _out({"error": exc.code, "detail": str(exc), **exc.details})
            sys.exit(2)
        except WorkflowValidationError as exc:
            _out({"error": "invalid_workflow", "errors": exc.errors})
            sys.exit(2)
        except KeyError as exc:
            _out({"error": "not_found", "detail": str(exc.args[0])})
            sys.exit(2)

    return wrapper


@main.group()
def workflows() -> None:
    """Manage workflow definitions."""


@workflows.command("list")
@click.pass_context
@_guard
def wf_list(ctx: click.Context) -> None:
    _out(_svc(ctx).registry.list_versions())


@workflows.command("validate")
@click.argument("file")
@click.pass_context
@_guard
def wf_validate(ctx: click.Context, file: str) -> None:
    wf = compile_workflow(parse_definition(Path(file).read_text()), _svc(ctx).hub.types)
    _out({"valid": True, "workflow": wf.name, "version": wf.version, "content_hash": wf.content_hash})


@workflows.command("publish")
@click.argument("file")
@click.option("--activate", is_flag=True, help="Activate immediately")
@click.option("--actor", default="cli")
@click.option("--reason", default="")
@click.pass_context
@_guard
def wf_publish(ctx: click.Context, file: str, activate: bool, actor: str, reason: str) -> None:
    svc = _svc(ctx)
    wf = svc.registry.register(parse_definition(Path(file).read_text()), actor=actor, activate=activate, reason=reason)
    _out({"workflow": wf.name, "version": wf.version, "active_version": svc.registry.active_version(wf.name)})


@workflows.command("activate")
@click.argument("name")
@click.argument("version", type=int)
@click.option("--actor", default="cli")
@click.option("--reason", default="")
@click.pass_context
@_guard
def wf_activate(ctx: click.Context, name: str, version: int, actor: str, reason: str) -> None:
    svc = _svc(ctx)
    svc.registry.activate(name, version, actor=actor, reason=reason)
    _out({"workflow": name, "active_version": svc.registry.active_version(name)})


@main.command()
@click.argument("workflow")
@click.argument("file")
@click.option("--key", required=True, help="Idempotency key")
@click.pass_context
@_guard
def submit(ctx: click.Context, workflow: str, file: str, key: str) -> None:
    res = _svc(ctx).submit(workflow, _load(file), key, actor="cli")
    _out({"http_status": res.http_status, "replayed": res.replayed, "duplicate_of": res.duplicate_of, **res.body})


@main.command()
@click.argument("request_id")
@click.pass_context
@_guard
def show(ctx: click.Context, request_id: str) -> None:
    _out(_svc(ctx).get(request_id))


@main.command()
@click.argument("request_id")
@click.pass_context
@_guard
def history(ctx: click.Context, request_id: str) -> None:
    _out(_svc(ctx).history(request_id))


@main.command()
@click.argument("request_id")
@click.pass_context
@_guard
def audit(ctx: click.Context, request_id: str) -> None:
    _out(_svc(ctx).audit(request_id))


@main.command()
@click.argument("request_id")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
@_guard
def explain(ctx: click.Context, request_id: str, as_json: bool) -> None:
    exp = _svc(ctx).explain(request_id)
    if as_json:
        _out(exp)
    else:
        click.echo(exp["narrative"])


@main.command()
@click.argument("request_id")
@click.pass_context
@_guard
def retry(ctx: click.Context, request_id: str) -> None:
    _out(_svc(ctx).retry(request_id, actor="cli"))


@main.command()
@click.argument("request_id")
@click.option("--approve/--reject", "approve", required=True)
@click.option("--reviewer", required=True)
@click.option("--comment", required=True)
@click.pass_context
@_guard
def review(ctx: click.Context, request_id: str, approve: bool, reviewer: str, comment: str) -> None:
    _out(_svc(ctx).review(request_id, "approve" if approve else "reject", reviewer, comment))


@main.command()
@click.option("--once", is_flag=True, help="Run one tick and exit")
@click.option("--interval", default=10.0, help="Seconds between ticks")
@click.pass_context
@_guard
def worker(ctx: click.Context, once: bool, interval: float) -> None:
    """Resume due retries, recover stale requests, relay the outbox."""
    svc = _svc(ctx)
    while True:
        try:
            _out({"retries": svc.process_due_retries(), "outbox": svc.relay_outbox()})
        except Exception as exc:
            _out({"worker_error": f"{type(exc).__name__}: {exc}"})
            if once:
                sys.exit(1)
        if once:
            return
        time.sleep(interval)


@main.command()
@click.argument("workflow")
@click.argument("file")
@click.option("--version", type=int, default=None)
@click.pass_context
@_guard
def simulate(ctx: click.Context, workflow: str, file: str, version: int | None) -> None:
    res = _svc(ctx).simulate(workflow, _load(file), version)
    for p in res["path"]:
        p.pop("trace", None)
    _out(res)


@main.command()
@click.argument("workflow")
@click.option("--from", "from_version", type=int, required=True)
@click.option("--to", "to_version", type=int, required=True)
@click.pass_context
@_guard
def impact(ctx: click.Context, workflow: str, from_version: int, to_version: int) -> None:
    _out(_svc(ctx).impact(workflow, from_version, to_version))


@main.command("audit-verify")
@click.pass_context
@_guard
def audit_verify(ctx: click.Context) -> None:
    res = _svc(ctx).verify_audit()
    _out(res)
    sys.exit(0 if res["ok"] else 1)


@main.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8000, type=int)
def serve(host: str, port: int) -> None:
    import uvicorn

    uvicorn.run("decision_platform.api.app:create_app", factory=True, host=host, port=port)


if __name__ == "__main__":
    main()
