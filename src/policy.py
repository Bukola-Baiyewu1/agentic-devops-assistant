"""Deterministic safety policy: which tools exist, what arguments they accept,
and whether a model proposal is allowed to reach a human for approval.

The model can suggest. This module decides. Nothing the model writes, and
nothing written in an alert or a runbook, can change these rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import settings
from .rag import Chunk

SERVICE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
_SERVICE_RE = re.compile(SERVICE_PATTERN)


# ------------------------------------------------------------ tool arguments
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceArgs(_Strict):
    service: str = Field(pattern=SERVICE_PATTERN)


class LogsArgs(_Strict):
    service: str = Field(pattern=SERVICE_PATTERN)
    lines: int = Field(default=50, ge=1, le=200)


class SearchArgs(_Strict):
    query: str = Field(min_length=1, max_length=500)


class DiagnosticArgs(_Strict):
    command: str = Field(min_length=1, max_length=100)


class ScaleArgs(_Strict):
    service: str = Field(pattern=SERVICE_PATTERN)
    replicas: int = Field(ge=1, le=100)


READ_TOOL_ARGS: dict[str, type[BaseModel]] = {
    "get_service_health": ServiceArgs,
    "get_recent_logs": LogsArgs,
    "search_runbooks": SearchArgs,
    "run_diagnostic": DiagnosticArgs,
}

ACTION_TOOL_ARGS: dict[str, type[BaseModel]] = {
    "restart_service": ServiceArgs,
    "scale_service": ScaleArgs,
}

ALLOWED_DIAGNOSTICS = frozenset({"uptime", "free", "df", "ps"})


# ------------------------------------------------------------------ proposal
class Proposal(BaseModel):
    """The planner's final answer. Exactly one action, or an escalation."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["propose_action", "escalate"]
    reasoning: str = Field(max_length=4000)
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    citation_chunk_id: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


def normalize_args(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    schema = ACTION_TOOL_ARGS.get(tool) or READ_TOOL_ARGS.get(tool)
    if schema is None:
        raise ValueError(f"unknown tool '{tool}'")
    return schema(**args).model_dump()


def args_hash(tool: str, args: dict[str, Any]) -> str:
    """Stable fingerprint of a tool call, used to bind an approval to it."""
    canonical = json.dumps({"tool": tool, "args": normalize_args(tool, args)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def is_valid_service_name(name: str) -> bool:
    return bool(_SERVICE_RE.match(name))


def service_allowed(name: str) -> bool:
    return is_valid_service_name(name) and name in settings.allowed_services


def validate_proposal(
    proposal: Proposal,
    *,
    alert_service: str,
    retrieved: dict[str, Chunk],
    health: dict | None,
) -> list[str]:
    """Return the reasons a proposal must be rejected (empty list = allowed)."""
    if proposal.decision == "escalate":
        return []

    reasons: list[str] = []
    tool = proposal.tool_name
    if not tool or tool not in ACTION_TOOL_ARGS:
        return [f"tool '{tool}' is not an allowed action"]

    try:
        args = normalize_args(tool, proposal.tool_args)
    except (ValidationError, ValueError, TypeError):
        return ["tool arguments failed the strict schema"]

    if args["service"] != alert_service:
        reasons.append("proposal targets a different service than the alert")
    if not service_allowed(args["service"]):
        reasons.append(f"service '{args['service']}' is not in the allowed target list")

    if tool == "scale_service":
        current = int((health or {}).get("replicas", 0))
        if args["replicas"] != current + 1:
            reasons.append("scale_service may only add exactly one replica")
        if args["replicas"] > settings.max_replicas:
            reasons.append(f"replica count exceeds the policy maximum of {settings.max_replicas}")

    cid = proposal.citation_chunk_id
    if not cid:
        reasons.append("no runbook citation")
    elif cid not in retrieved:
        reasons.append("citation is not one of the retrieved runbook passages")
    elif tool not in retrieved[cid].text:
        reasons.append("cited passage does not support this tool")

    if proposal.confidence < settings.min_confidence:
        reasons.append(f"confidence {proposal.confidence} is below the threshold {settings.min_confidence}")
    return reasons
