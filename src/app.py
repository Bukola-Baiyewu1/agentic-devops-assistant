"""The web service: webhook ingress, approval UI and API, events, traces,
metrics, and demo controls for the simulated service.

Run it with:  uvicorn src.app:app --reload
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text

from . import approval, events
from .config import settings, validate_for_startup
from .demo import fleet, planner_faults
from .observability import (
    get_correlation_id,
    log,
    new_correlation_id,
    registry,
    reset_correlation_id,
    set_correlation_id,
)
from .policy import SERVICE_PATTERN
from .rag import get_retriever
from .security import (
    ApprovalError,
    check_password,
    current_challenge_token,
    limiter,
    verify_webhook_signature,
)
from .state import ConflictError, store
from .states import InvalidTransition
from .tracing import get_tracer


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    validate_for_startup()
    log("api_started", env=settings.env, planner=settings.planner_mode(), langfuse=get_tracer().enabled)
    yield
    get_tracer().flush()


app = FastAPI(
    title="Aegis - Agentic DevOps Assistant",
    description="Triage alerts, cite runbooks, and act only after human approval.",
    version="1.0.0",
    lifespan=lifespan,
)
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# ----------------------------------------------------------------- middleware
_CID_OK = re.compile(r"^[A-Za-z0-9\-]{8,64}$")
_UNLIMITED = {"/health", "/ready", "/metrics"}
_JSON_ONLY_PREFIXES = ("/actions", "/events", "/demo")


@app.middleware("http")
async def platform_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    incoming = request.headers.get("x-correlation-id", "")
    cid = incoming if _CID_OK.match(incoming) else new_correlation_id()
    token = set_correlation_id(cid)
    try:
        path = request.url.path
        if path not in _UNLIMITED:
            client = request.client.host if request.client else "unknown"
            group = path.split("/")[1] if "/" in path else path
            if not limiter.allow((client, group), settings.rate_limit_per_minute):
                log("rate_limited", client=client, path=path)
                response: Response = JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
                response.headers["Retry-After"] = "60"
                response.headers["X-Correlation-ID"] = cid
                return response
        # CSRF defence: browsers cannot send a cross-site JSON POST without a
        # CORS preflight, which this API never approves.
        is_json = request.headers.get("content-type", "").startswith("application/json")
        if request.method == "POST" and path.startswith(_JSON_ONLY_PREFIXES) and not is_json:
            response = JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
            response.headers["X-Correlation-ID"] = cid
            return response
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = cid
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return response
    finally:
        reset_correlation_id(token)


# ---------------------------------------------------------------------- auth
_basic = HTTPBasic(auto_error=False, realm="Aegis approvals")


def current_user(creds: Annotated[HTTPBasicCredentials | None, Depends(_basic)]) -> str:
    if creds is None or not check_password(creds.username, creds.password):
        raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": 'Basic realm="Aegis"'})
    return creds.username


User = Annotated[str, Depends(current_user)]


def _translate(fn: Callable[[], Any]) -> Any:
    """Map domain errors to HTTP status codes in one place."""
    try:
        return fn()
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e
    except approval.InvalidApprovalToken as e:
        raise HTTPException(403, str(e)) from e
    except ApprovalError as e:
        raise HTTPException(403, str(e)) from e
    except approval.ApprovalExpired as e:
        raise HTTPException(410, str(e)) from e
    except (InvalidTransition, ConflictError) as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# -------------------------------------------------------------------- health
@app.get("/health", tags=["platform"])
def health() -> dict:
    return {"ok": True}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request) -> HTMLResponse:
    """Public landing page: what Aegis is, with links to the API docs and source."""
    nonce = secrets.token_urlsafe(16)
    response = templates.TemplateResponse(
        request,
        "home.html",
        {"csp_nonce": nonce, "planner": settings.planner_mode(), "chunks": len(get_retriever().chunks)},
    )
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    return response


@app.get("/ready", tags=["platform"])
def ready() -> dict:
    try:
        with store.engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception:
        raise HTTPException(503, "database unavailable") from None
    return {"ok": True, "planner": settings.planner_mode(), "runbook_chunks": len(get_retriever().chunks)}


@app.get("/metrics", tags=["platform"], include_in_schema=False)
def metrics(request: Request) -> Response:
    if settings.metrics_token:
        expected = f"Bearer {settings.metrics_token}"
        if not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "metrics token required")
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# ----------------------------------------------------------- 1) alert ingress
class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:\-]+$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    service: str = Field(default="web", pattern=SERVICE_PATTERN)


def _summary(action: dict) -> dict:
    p = action["proposal"]
    return {
        "action_id": action["id"],
        "status": action["status"],
        "plan": p["reasoning"],
        "decision": p["decision"],
        "citation": (action.get("citation") or {}).get("source"),
        "citation_detail": action.get("citation"),
        "proposed_tool": action.get("tool_name"),
        "proposed_args": action.get("tool_args") or None,
        "approve_url": f"/approve/{action['id']}" if action["status"] == "pending_approval" else None,
    }


def _event_response(event: dict, duplicate: bool) -> JSONResponse:
    body: dict[str, Any] = {
        "event_id": event["event_id"],
        "event_status": event["status"],
        "duplicate": duplicate,
        "attempts": event["attempts"],
    }
    if event["action_id"]:
        action = store.get_action(event["action_id"])
        if action:
            body.update(_summary(action))
        return JSONResponse(body, status_code=200)
    body["action_id"] = None
    return JSONResponse(body, status_code=202)


@app.post("/webhook/alert", tags=["ingress"])
async def webhook_alert(request: Request) -> JSONResponse:
    raw = await request.body()
    if not verify_webhook_signature(raw, request.headers.get("x-aegis-signature")):
        log("webhook_signature_rejected")
        raise HTTPException(401, "invalid webhook signature")
    try:
        alert = Alert.model_validate_json(raw)
    except ValidationError as e:
        detail = e.errors(include_url=False, include_context=False, include_input=False)
        raise HTTPException(422, detail) from e

    created, event = events.ingest(alert.model_dump(), trace_id=get_correlation_id())
    if created and settings.process_inline:
        event = events.process_event(alert.event_id)
    return _event_response(event, duplicate=not created)


# ------------------------------------------------------- 2) actions + approval
class TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
    execute: bool = True


class DenyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=500)


class RollbackApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=200)
    execute: bool = True


@app.get("/actions", tags=["actions"])
def list_actions(user: User) -> list[dict]:
    return [approval.public_view(a) for a in store.list_actions()]


@app.get("/actions/{action_id}", tags=["actions"])
def get_action(action_id: str, user: User) -> dict:
    return approval.public_view(_translate(lambda: approval._get(action_id)))


@app.get("/actions/{action_id}/audit", tags=["actions"])
def get_audit(action_id: str, user: User) -> list[dict]:
    _translate(lambda: approval._get(action_id))
    return store.list_audit(action_id)


@app.get("/actions/{action_id}/challenge", tags=["actions"])
def get_challenge(action_id: str, user: User) -> dict:
    """The current approval challenge, for authenticated approvers (CLI use)."""
    action = _translate(lambda: approval._get(action_id))
    ch = action.get("challenge")
    if not ch:
        raise HTTPException(409, f"no decision is open for this action (status '{action['status']}')")
    log("challenge_issued", action_id=action_id, user=user, scope=ch["scope"])
    return {"scope": ch["scope"], "token": current_challenge_token(action, ch["scope"]), "expires_at": ch["expires_at"]}


@app.post("/actions/{action_id}/approve", tags=["actions"])
def approve_action(action_id: str, body: TokenBody, user: User) -> dict:
    result = _translate(lambda: approval.approve(action_id, token=body.token, approver=user, execute=body.execute))
    capability = result.pop("capability", None)
    view = approval.public_view(result)
    if capability:  # returned exactly once, to the approver, for an MCP client
        view["capability"] = capability
    return view


@app.post("/actions/{action_id}/deny", tags=["actions"])
def deny_action(action_id: str, body: DenyBody, user: User) -> dict:
    return approval.public_view(
        _translate(lambda: approval.deny(action_id, token=body.token, approver=user, reason=body.reason))
    )


@app.post("/actions/{action_id}/rollback", tags=["actions"])
def request_rollback(action_id: str, user: User) -> dict:
    """Step 1 of rollback: open a rollback approval. The service is unchanged."""
    view = approval.public_view(_translate(lambda: approval.request_rollback(action_id, requester=user)))
    view["approve_url"] = f"/approve/{action_id}"
    return view


@app.post("/actions/{action_id}/rollback/approve", tags=["actions"])
def approve_rollback(action_id: str, body: RollbackApproveBody, user: User) -> dict:
    """Step 2 of rollback: approve with the fresh rollback challenge."""
    result = _translate(
        lambda: approval.approve_rollback(action_id, token=body.token, approver=user, execute=body.execute)
    )
    capability = result.pop("capability", None)
    view = approval.public_view(result)
    if capability:
        view["capability"] = capability
    return view


# --------------------------------------------------------- 3) approval page
@app.get("/approve/{action_id}", response_class=HTMLResponse, tags=["actions"])
def approve_page(request: Request, action_id: str, user: User) -> HTMLResponse:
    action = _translate(lambda: approval._get(action_id))
    citation = action.get("citation") or {}
    chunk = get_retriever().get(citation.get("chunk_id", "")) if citation else None
    nonce = secrets.token_urlsafe(16)
    response = templates.TemplateResponse(
        request,
        "approve.html",
        {
            "action": approval.public_view(action),
            "user": user,
            "citation_text": chunk.text if chunk else None,
            "approve_token": current_challenge_token(action, "approve"),
            "rollback_token": current_challenge_token(action, "rollback"),
            "csp_nonce": nonce,
        },
    )
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------- 4) events + traces
@app.get("/events", tags=["events"])
def list_events(user: User, status: Annotated[str | None, Query(max_length=32)] = None) -> list[dict]:
    return store.list_events(status=status)


@app.get("/events/{event_id}", tags=["events"])
def get_event(event_id: str, user: User) -> dict:
    event = store.get_event(event_id)
    if not event:
        raise HTTPException(404, "event not found")
    return event


@app.post("/events/{event_id}/replay", tags=["events"])
def replay_event(event_id: str, user: User) -> dict:
    event = _translate(lambda: events.replay(event_id, actor=user))
    if settings.process_inline:
        event = events.process_event(event_id)
    return event


@app.get("/traces", tags=["observability"])
def traces(user: User) -> list[dict]:
    return store.list_traces()


# ----------------------------------------------------- 5) demo service controls
class FaultBody(BaseModel):
    count: int = Field(ge=0, le=20)
    kind: str = Field(default="transient", pattern="^(transient|permanent)$")


@app.get("/demo/health", tags=["demo"])
def demo_health(service: Annotated[str, Query(pattern=SERVICE_PATTERN)] = "web") -> dict:
    if service not in settings.allowed_services:
        raise HTTPException(404, "unknown service")
    return fleet.get(service).health()


@app.get("/demo/logs", tags=["demo"])
def demo_logs(user: User) -> dict:
    return {"logs": fleet.get("web").logs(50)}


@app.post("/demo/break", tags=["demo"])
def demo_break(user: User) -> dict:
    fleet.get("web").inject_error()
    return fleet.get("web").health()


@app.post("/demo/overload", tags=["demo"])
def demo_overload(user: User) -> dict:
    fleet.get("web").overload()
    return fleet.get("web").health()


@app.post("/demo/fix", tags=["demo"])
def demo_fix(user: User) -> dict:
    fleet.get("web").clear_error()
    return fleet.get("web").health()


@app.post("/demo/planner-faults", tags=["demo"])
def demo_planner_faults(body: FaultBody, user: User) -> dict:
    """Make the next N planning attempts fail, to demonstrate retries and dead letters."""
    planner_faults.set(body.count, body.kind)
    return {"next_failures": body.count, "kind": body.kind}
