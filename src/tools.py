"""The agent's capabilities.

READ tools are always allowed and never change state.

ACTION tools change the (simulated) service. Each one spends a scoped,
single-use execution capability before doing anything, so there is no code
path - HTTP, background worker, or MCP client - that can act without a human
approval of that exact call. This is defense in depth: the approval workflow
checks first, and the tool checks again.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .demo import fleet
from .observability import TOOL_EXECUTIONS, TOOL_LATENCY, Timer, log
from .policy import ALLOWED_DIAGNOSTICS, LogsArgs, ScaleArgs, SearchArgs, ServiceArgs, service_allowed
from .rag import get_retriever
from .security import ApprovalError, spend_capability

__all__ = [
    "ApprovalError",
    "get_service_health",
    "get_recent_logs",
    "search_runbooks",
    "run_diagnostic",
    "restart_service",
    "scale_service",
    "rollback_service",
    "READ_TOOLS",
    "ACTION_TOOLS",
]


def _require_service(service: str) -> str:
    ServiceArgs(service=service)
    if not service_allowed(service):
        raise ValueError(f"service '{service}' is not in the allowed target list")
    return service


# ---- READ tools (no approval needed) ----------------------------------------
def get_service_health(service: str = "web") -> dict:
    return fleet.get(_require_service(service)).health()


def get_recent_logs(service: str = "web", lines: int = 50) -> str:
    args = LogsArgs(service=service, lines=lines)
    return fleet.get(_require_service(args.service)).logs(args.lines)


def search_runbooks(query: str) -> list[dict]:
    args = SearchArgs(query=query)
    return [
        {
            "chunk_id": r.chunk.chunk_id,
            "source": r.chunk.source,
            "heading": r.chunk.heading,
            "lines": f"{r.chunk.start_line}-{r.chunk.end_line}",
            "text": r.chunk.text,
            "score": r.score,
        }
        for r in get_retriever().retrieve(args.query)
    ]


def run_diagnostic(command: str) -> str:
    parts = command.strip().split()
    base = parts[0] if parts else ""
    if base not in ALLOWED_DIAGNOSTICS or len(parts) > 1:
        return f"refused: only these argument-free read-only diagnostics are allowed: {sorted(ALLOWED_DIAGNOSTICS)}"
    return f"[diagnostic:{base}] ok (simulated)"


READ_TOOLS: dict[str, Callable[..., Any]] = {
    "get_service_health": get_service_health,
    "get_recent_logs": get_recent_logs,
    "search_runbooks": search_runbooks,
    "run_diagnostic": run_diagnostic,
}


# ---- ACTION tools (require a capability) ------------------------------------
def _run(tool: str, fn: Callable[[], dict]) -> dict:
    with Timer() as t:
        try:
            result = fn()
        except Exception:
            TOOL_EXECUTIONS.labels(tool=tool, result="error").inc()
            raise
    TOOL_LATENCY.labels(tool=tool).observe(t.seconds)
    TOOL_EXECUTIONS.labels(tool=tool, result="ok").inc()
    log("tool_executed", tool=tool, latency_ms=t.ms)
    return result


def restart_service(service: str, capability: str | None = None, action_id: str | None = None) -> dict:
    args = ServiceArgs(service=service).model_dump()
    _require_service(service)
    spend_capability(
        capability, scope="execute", tool="restart_service", args=args, service=service, action_id=action_id
    )

    def go() -> dict:
        target = fleet.get(service)
        target.restart()
        return {"action": "restart_service", "service": service, "result": target.health()}

    return _run("restart_service", go)


def scale_service(service: str, replicas: int, capability: str | None = None, action_id: str | None = None) -> dict:
    args = ScaleArgs(service=service, replicas=replicas).model_dump()
    _require_service(service)
    spend_capability(capability, scope="execute", tool="scale_service", args=args, service=service, action_id=action_id)

    def go() -> dict:
        target = fleet.get(service)
        target.scale(replicas)
        return {"action": "scale_service", "service": service, "replicas": replicas, "result": target.health()}

    return _run("scale_service", go)


def rollback_service(service: str, action_id: str, prior_state: dict, capability: str | None = None) -> dict:
    _require_service(service)
    spend_capability(capability, scope="rollback", tool="rollback", args={}, service=service, action_id=action_id)

    def go() -> dict:
        target = fleet.get(service)
        target.restore(prior_state)
        return {"action": "rollback", "service": service, "result": target.health()}

    return _run("rollback", go)


ACTION_TOOLS: dict[str, Callable[..., dict]] = {
    "restart_service": restart_service,
    "scale_service": scale_service,
}
