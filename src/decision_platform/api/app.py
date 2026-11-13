"""REST API (FastAPI). Thin transport layer over DecisionService.

Errors are returned as RFC 9457 / RFC 7807 ``application/problem+json``.
Interactive docs: /docs (Swagger UI) and /redoc.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import Body, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config.compiler import WorkflowValidationError
from ..config.registry import UnknownWorkflowError, VersionConflictError, parse_definition
from ..domain.models import IllegalTransition
from ..persistence.db import Database
from ..persistence.repository import ConcurrencyConflict, NotFound
from ..service import (
    DecisionService,
    DuplicateRequest,
    IdempotencyKeyMissing,
    IdempotencyKeyReuse,
    InputValidationError,
    InvalidState,
    RequestInProgress,
    ServiceError,
)

_STATUS = {
    InputValidationError: 422,
    IdempotencyKeyMissing: 400,
    IdempotencyKeyReuse: 422,  # per IETF draft-ietf-httpapi-idempotency-key-header
    RequestInProgress: 409,
    DuplicateRequest: 409,
    InvalidState: 409,
}


def _problem(status: int, title: str, detail: str, code: str, **extra: Any) -> JSONResponse:
    body = {
        **extra,
        "type": f"https://decision-platform.local/problems/{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": code,
    }
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


class ReviewBody(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(..., min_length=1)
    comment: str = Field(..., min_length=3)


class FaultBody(BaseModel):
    fail_next: list[Literal["transient", "timeout", "permanent"]] | None = None
    failure_rate: float | None = Field(None, ge=0, le=1)
    down: bool | None = None
    latency_ms: int | None = Field(None, ge=0)


class SimulateBody(BaseModel):
    input: dict[str, Any]
    version: int | None = None
    recorded_outputs: dict[str, Any] | None = None
    evaluation_date: str | None = None


def create_app(service: DecisionService | None = None) -> FastAPI:
    if service is None:
        db = Database(os.environ.get("DP_DB_PATH", "data/decisions.db"))
        service = DecisionService(db)
        service.load_workflows(os.environ.get("DP_CONFIG_DIR", str(Path("config/workflows"))))
    svc = service

    app = FastAPI(
        title="Configurable Workflow Decision Platform",
        version="1.0.0",
        description="Config-driven rules and workflow engine with idempotency, retries, audit trail and explanations.",
    )
    app.state.service = svc

    # Error mapping
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        status = _STATUS.get(type(exc), 400)
        return _problem(status, type(exc).__name__, str(exc), exc.code, **exc.details)

    @app.exception_handler(UnknownWorkflowError)
    async def _unknown_wf(_: Request, exc: UnknownWorkflowError) -> JSONResponse:
        return _problem(404, "Unknown workflow", str(exc.args[0]), "unknown_workflow")

    @app.exception_handler(NotFound)
    async def _not_found(_: Request, exc: NotFound) -> JSONResponse:
        return _problem(404, "Not found", str(exc.args[0]), "not_found")

    @app.exception_handler(WorkflowValidationError)
    async def _bad_config(_: Request, exc: WorkflowValidationError) -> JSONResponse:
        return _problem(422, "Invalid workflow definition", str(exc), "invalid_workflow", errors=exc.errors)

    @app.exception_handler(VersionConflictError)
    async def _version_conflict(_: Request, exc: VersionConflictError) -> JSONResponse:
        return _problem(409, "Version conflict", str(exc), "version_conflict")

    @app.exception_handler(ConcurrencyConflict)
    async def _conflict(_: Request, exc: ConcurrencyConflict) -> JSONResponse:
        return _problem(409, "Concurrent modification", str(exc), "concurrent_modification")

    @app.exception_handler(IllegalTransition)
    async def _illegal(_: Request, exc: IllegalTransition) -> JSONResponse:
        return _problem(409, "Illegal state transition", str(exc), "illegal_transition")

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        return _problem(
            500,
            "Internal error",
            f"{type(exc).__name__}: progress up to the last completed stage is "
            "saved; retrying with the same Idempotency-Key after the lease expires resumes from there",
            "internal_error",
        )

    # Health
    @app.get("/health", tags=["ops"])
    def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/ready", tags=["ops"])
    def ready() -> dict[str, Any]:
        svc.db.read().execute("SELECT 1").fetchone()
        return {"status": "ready", "workflows": len(svc.registry.list_versions())}

    # Requests
    @app.post("/v1/workflows/{workflow}/requests", status_code=201, tags=["requests"])
    def submit(
        workflow: str,
        response: Response,
        payload: Any = Body(...),
        idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        """Submit a request. Requires an `Idempotency-Key` header; retries with the same key and payload are safe."""
        result = svc.submit(workflow, payload, idempotency_key)
        response.status_code = result.http_status
        for k, v in result.headers.items():
            response.headers[k] = v
        return {**result.body, "replayed": result.replayed, "duplicate_of": result.duplicate_of}

    @app.get("/v1/requests", tags=["requests"])
    def list_requests(
        workflow: str | None = None, status: str | None = None, limit: int = Query(50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        return svc.list_requests(workflow, status, limit)

    @app.get("/v1/requests/{request_id}", tags=["requests"])
    def get_request(request_id: str) -> dict[str, Any]:
        return svc.get(request_id)

    @app.get("/v1/requests/{request_id}/history", tags=["requests"])
    def history(request_id: str) -> dict[str, Any]:
        return svc.history(request_id)

    @app.get("/v1/requests/{request_id}/audit", tags=["audit"])
    def audit(request_id: str) -> list[dict[str, Any]]:
        return svc.audit(request_id)

    @app.get("/v1/requests/{request_id}/explanation", tags=["audit"])
    def explanation(request_id: str, format: Literal["json", "text"] = "json") -> Any:
        exp = svc.explain(request_id)
        if format == "text":
            return Response(exp["narrative"], media_type="text/plain")
        return exp

    @app.post("/v1/requests/{request_id}/retry", tags=["requests"])
    def retry(request_id: str) -> dict[str, Any]:
        return svc.retry(request_id)

    @app.post("/v1/requests/{request_id}/review", tags=["requests"])
    def review(request_id: str, body: ReviewBody) -> dict[str, Any]:
        return svc.review(request_id, body.decision, body.reviewer, body.comment)

    # Workflows
    @app.get("/v1/workflows", tags=["workflows"])
    def list_workflows() -> list[dict[str, Any]]:
        return svc.registry.list_versions()

    @app.get("/v1/workflows/{workflow}", tags=["workflows"])
    def get_workflow(workflow: str, version: int | None = None) -> dict[str, Any]:
        wf = svc.registry.get(workflow, version) if version else svc.registry.active(workflow)
        return {
            "workflow": wf.name,
            "version": wf.version,
            "content_hash": wf.content_hash,
            "active_version": svc.registry.active_version(workflow),
            "activations": svc.registry.activation_history(workflow),
            "definition": wf.raw,
        }

    @app.post("/v1/workflows", status_code=201, tags=["workflows"])
    async def publish_workflow(
        request: Request, activate: bool = False, actor: str = "api", reason: str = ""
    ) -> dict[str, Any]:
        """Publish a new workflow version (YAML or JSON body). Validated before it is stored."""
        raw = parse_definition((await request.body()).decode())
        wf = svc.registry.register(raw, actor=actor, activate=activate, reason=reason)
        return {
            "workflow": wf.name,
            "version": wf.version,
            "content_hash": wf.content_hash,
            "active_version": svc.registry.active_version(wf.name) if activate else None,
        }

    @app.post("/v1/workflows/validate", tags=["workflows"])
    async def validate_workflow(request: Request) -> dict[str, Any]:
        """Validate a definition without storing it."""
        from ..config.compiler import compile_workflow

        wf = compile_workflow(parse_definition((await request.body()).decode()), svc.hub.types)
        return {"valid": True, "workflow": wf.name, "version": wf.version, "content_hash": wf.content_hash}

    @app.post("/v1/workflows/{workflow}/versions/{version}/activate", tags=["workflows"])
    def activate(workflow: str, version: int, actor: str = "api", reason: str = "") -> dict[str, Any]:
        svc.registry.activate(workflow, version, actor=actor, reason=reason)
        return {"workflow": workflow, "active_version": svc.registry.active_version(workflow)}

    @app.post("/v1/workflows/{workflow}/simulate", tags=["workflows"])
    def simulate(workflow: str, body: SimulateBody) -> dict[str, Any]:
        """Dry run: evaluate a payload without persisting anything."""
        return svc.simulate(workflow, body.input, body.version, body.recorded_outputs, body.evaluation_date)

    @app.get("/v1/workflows/{workflow}/impact", tags=["workflows"])
    def impact(
        workflow: str, from_version: int, to_version: int, limit: int = Query(500, ge=1, le=10000)
    ) -> dict[str, Any]:
        """Replay historical requests against another version and report decisions that would change."""
        return svc.impact(workflow, from_version, to_version, limit)

    # Admin / ops
    @app.post("/v1/admin/retries/run", tags=["ops"])
    def run_retries() -> dict[str, Any]:
        return svc.process_due_retries()

    @app.post("/v1/admin/outbox/relay", tags=["ops"])
    def relay() -> dict[str, Any]:
        return svc.relay_outbox()

    @app.get("/v1/admin/audit/verify", tags=["audit"])
    def verify() -> dict[str, Any]:
        return svc.verify_audit()

    @app.get("/v1/admin/dependencies", tags=["ops"])
    def dependencies() -> dict[str, Any]:
        return svc.hub.status()

    @app.put("/v1/admin/dependencies/{dep_type}/faults", tags=["ops"])
    def set_faults(dep_type: str, body: FaultBody) -> dict[str, Any]:
        """Fault injection for demos and chaos testing of a simulated dependency."""
        try:
            adapter = svc.hub.get(dep_type)
        except KeyError as exc:
            raise NotFound(str(exc.args[0])) from None
        adapter.configure(**body.model_dump())
        return svc.hub.status()[dep_type]

    @app.post("/v1/admin/dependencies/reset", tags=["ops"])
    def reset_dependencies() -> dict[str, Any]:
        svc.hub.reset()
        return svc.hub.status()

    return app
