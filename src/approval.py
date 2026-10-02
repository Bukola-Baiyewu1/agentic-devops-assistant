"""Human-in-the-loop workflow: approve, deny, execute, roll back, expire.

Lifecycle (see states.py for the full table):

    pending_approval -> executing -> executed -> rollback_pending -> rolling_back -> rolled_back
                     -> approved (execution deferred to an MCP client) -> executing
                     -> denied | expired
    executing -> failed ; rolling_back -> rollback_failed

Rules enforced here:
* A decision needs an authenticated approver AND the action's current
  approval challenge for the right scope (`approve` or `rollback`).
* Every status change is a compare-and-set, so a second concurrent approval
  of the same action fails instead of executing twice.
* After a decision the challenge is cleared, so it cannot be replayed.
* Rollback needs its own fresh challenge and capability; the original
  approval cannot authorize it.
* Every record keeps who decided, when, the prior state, and the new state.
"""

from __future__ import annotations

import time
from typing import Any

from . import tools
from .config import settings
from .demo import fleet
from .observability import APPROVALS, ROLLBACKS, log
from .policy import normalize_args
from .security import (
    ApprovalError,
    challenge_expired,
    check_capability,
    mint_capability,
    new_challenge,
    verify_challenge,
)
from .state import ConflictError, store
from .states import ActionStatus as S
from .states import InvalidTransition
from .tracing import get_tracer


class InvalidApprovalToken(PermissionError):
    pass


class ApprovalExpired(RuntimeError):
    pass


def _now() -> float:
    return time.time()


# ------------------------------------------------------------------- create
def create_pending_action(alert: dict, plan_result: dict, event_id: str | None = None, actor: str = "agent") -> dict:
    proposal = plan_result["proposal"]
    escalated = proposal["decision"] != "propose_action" or not proposal.get("tool_name")
    status = S.ESCALATED if escalated else S.PENDING_APPROVAL
    data: dict[str, Any] = {
        "alert": alert,
        "proposal": proposal,
        "citation": plan_result.get("citation"),
        "health_at_plan": plan_result.get("health_at_plan"),
        "trace": plan_result["trace"],
        "requested_at": _now(),
        "challenge": None if escalated else new_challenge("approve"),
        "tool_name": proposal.get("tool_name"),
        "tool_args": normalize_args(proposal["tool_name"], proposal["tool_args"]) if not escalated else {},
        "approver": None,
        "decision": None,
        "decided_at": None,
        "executed_at": None,
        "prior_state": None,
        "new_state": None,
        "result": None,
        "error": None,
        "rollback": None,
    }
    action = store.create_action(
        service=alert.get("service", "web"), status=status, event_id=event_id, data=data, actor=actor
    )
    store.add_trace(plan_result["trace"], action_id=action["id"], event_id=event_id)
    log("action_created", action_id=action["id"], status=status)
    return action


# -------------------------------------------------------------- helpers
def _get(action_id: str) -> dict:
    action = store.get_action(action_id)
    if action is None:
        raise KeyError("action not found")
    return action


def _expire_if_needed(action: dict, actor: str) -> None:
    if challenge_expired(action):
        try:
            store.transition(
                action["id"],
                to=S.EXPIRED,
                actor=actor,
                event="expired",
                expected=[action["status"]],
                changes={"challenge": None},
            )
        except (InvalidTransition, ConflictError):
            pass
        raise ApprovalExpired("the approval window has expired; a new alert is required")


def _check_decision(action: dict, scope: str, expected_status: str, token: str, actor: str) -> None:
    if action["status"] != expected_status:
        raise InvalidTransition(action["status"], "decision")
    _expire_if_needed(action, actor)
    if not verify_challenge(action, scope, token):
        log("invalid_approval_token", action_id=action["id"], actor=actor, scope=scope)
        raise InvalidApprovalToken("invalid approval token")


# ------------------------------------------------------------- decisions
def approve(action_id: str, *, token: str, approver: str, execute: bool = True) -> dict:
    """Approve a pending action. Executes immediately unless `execute=False`,
    in which case a capability is returned for an MCP client to use once."""
    action = _get(action_id)
    _check_decision(action, "approve", S.PENDING_APPROVAL, token, approver)

    tool, args = action["tool_name"], action["tool_args"]
    capability = mint_capability(
        action_id=action_id, scope="execute", tool=tool, args=args, service=action["service"], approver=approver
    )
    decided = {"challenge": None, "approver": approver, "decision": "approved", "decided_at": _now()}
    with get_tracer().observe("human-approval", metadata={"action_id": action_id, "approver": approver}):
        if not execute:
            updated = store.transition(
                action_id,
                to=S.APPROVED,
                actor=approver,
                event="approved",
                expected=[S.PENDING_APPROVAL],
                changes={**decided, "deferred_expires_at": _now() + settings.capability_ttl_seconds},
            )
            APPROVALS.labels(decision="approved").inc()
            log("action_approved", action_id=action_id, approver=approver, deferred=True)
            return {**updated, "capability": capability}

        store.transition(
            action_id,
            to=S.EXECUTING,
            actor=approver,
            event="approved",
            expected=[S.PENDING_APPROVAL],
            changes=decided,
        )
    APPROVALS.labels(decision="approved").inc()
    log("action_approved", action_id=action_id, approver=approver, deferred=False)
    return _execute(action_id, capability, actor=approver)


def deny(action_id: str, *, token: str, approver: str, reason: str = "") -> dict:
    action = _get(action_id)
    _check_decision(action, "approve", S.PENDING_APPROVAL, token, approver)
    updated = store.transition(
        action_id,
        to=S.DENIED,
        actor=approver,
        event="denied",
        expected=[S.PENDING_APPROVAL],
        changes={
            "challenge": None,
            "approver": approver,
            "decision": "denied",
            "decided_at": _now(),
            "denial_reason": reason[:500],
        },
    )
    APPROVALS.labels(decision="denied").inc()
    log("action_denied", action_id=action_id, approver=approver)
    return updated


# -------------------------------------------------------------- execution
def _execute(action_id: str, capability: str, actor: str) -> dict:
    action = _get(action_id)
    tool, args = action["tool_name"], dict(action["tool_args"])
    target = fleet.get(action["service"])
    prior = target.snapshot()
    with get_tracer().observe("execute-tool", as_type="tool", input={"tool": tool, "args": args}) as obs:
        try:
            result = tools.ACTION_TOOLS[tool](**args, capability=capability, action_id=action_id)
        except Exception as exc:
            obs.update(output={"error": type(exc).__name__}, level="ERROR")
            log("action_failed", action_id=action_id, tool=tool, error=str(exc))
            return store.transition(
                action_id,
                to=S.FAILED,
                actor=actor,
                event="execution_failed",
                expected=[S.EXECUTING],
                changes={
                    "error": f"{type(exc).__name__}: {exc}",
                    "prior_state": prior,
                    "new_state": target.snapshot(),
                    "executed_at": _now(),
                },
            )
        obs.update(output=result)
    updated = store.transition(
        action_id,
        to=S.EXECUTED,
        actor=actor,
        event="executed",
        expected=[S.EXECUTING],
        changes={"prior_state": prior, "new_state": target.snapshot(), "result": result, "executed_at": _now()},
    )
    log("action_executed", action_id=action_id, tool=tool)
    return updated


def execute_approved(action_id: str, *, tool: str, args: dict, capability: str, actor: str) -> dict:
    """Run an action that was approved with execution deferred (MCP path).

    The capability is checked BEFORE the status changes, so a wrong or
    replayed capability is refused without disturbing the approved action.
    """
    action = _get(action_id)
    if action["status"] != S.APPROVED:
        raise InvalidTransition(action["status"], S.EXECUTING)
    if tool != action["tool_name"]:
        raise ApprovalError("capability does not authorize this call (mismatch: tool)")
    check_capability(
        capability, scope="execute", tool=tool, args=args, service=args.get("service", ""), action_id=action_id
    )
    if normalize_args(tool, args) != action["tool_args"]:
        raise ApprovalError("capability does not authorize this call (mismatch: arguments)")
    store.transition(action_id, to=S.EXECUTING, actor=actor, event="execution_started", expected=[S.APPROVED])
    return _execute(action_id, capability, actor=actor)


# --------------------------------------------------------------- rollback
def request_rollback(action_id: str, *, requester: str) -> dict:
    """Step 1: ask for a rollback. Nothing changes on the service yet."""
    action = _get(action_id)
    if not action.get("prior_state"):
        raise InvalidTransition(action["status"], S.ROLLBACK_PENDING)
    updated = store.transition(
        action_id,
        to=S.ROLLBACK_PENDING,
        actor=requester,
        event="rollback_requested",
        expected=[S.EXECUTED],
        changes={
            "challenge": new_challenge("rollback"),
            "rollback": {"requested_by": requester, "requested_at": _now()},
        },
    )
    log("rollback_requested", action_id=action_id, requester=requester)
    return updated


def approve_rollback(action_id: str, *, token: str, approver: str, execute: bool = True) -> dict:
    """Step 2: a human approves the rollback with the fresh rollback challenge.

    With `execute=False` a rollback capability is returned for an MCP client.
    """
    action = _get(action_id)
    _check_decision(action, "rollback", S.ROLLBACK_PENDING, token, approver)
    capability = mint_capability(
        action_id=action_id, scope="rollback", tool="rollback", args={}, service=action["service"], approver=approver
    )
    rollback_info = {**(action.get("rollback") or {}), "approver": approver, "approved_at": _now()}
    if not execute:
        updated = store.transition(
            action_id,
            to=S.ROLLBACK_APPROVED,
            actor=approver,
            event="rollback_approved",
            expected=[S.ROLLBACK_PENDING],
            changes={
                "challenge": None,
                "rollback": rollback_info,
                "deferred_expires_at": _now() + settings.capability_ttl_seconds,
            },
        )
        return {**updated, "capability": capability}
    store.transition(
        action_id,
        to=S.ROLLING_BACK,
        actor=approver,
        event="rollback_approved",
        expected=[S.ROLLBACK_PENDING],
        changes={"challenge": None, "rollback": rollback_info},
    )
    return _rollback(action_id, capability, actor=approver)


def execute_approved_rollback(action_id: str, *, capability: str, actor: str) -> dict:
    """Run a rollback that was approved with execution deferred (MCP path)."""
    action = _get(action_id)
    if action["status"] != S.ROLLBACK_APPROVED:
        raise InvalidTransition(action["status"], S.ROLLING_BACK)
    check_capability(
        capability, scope="rollback", tool="rollback", args={}, service=action["service"], action_id=action_id
    )
    store.transition(
        action_id, to=S.ROLLING_BACK, actor=actor, event="rollback_started", expected=[S.ROLLBACK_APPROVED]
    )
    return _rollback(action_id, capability, actor=actor)


def _rollback(action_id: str, capability: str, actor: str) -> dict:
    action = _get(action_id)
    rollback_info = dict(action.get("rollback") or {})
    target = fleet.get(action["service"])
    before = target.snapshot()
    with get_tracer().observe("rollback", as_type="tool", metadata={"action_id": action_id}):
        try:
            result = tools.rollback_service(
                action["service"], action_id=action_id, prior_state=action["prior_state"], capability=capability
            )
        except Exception as exc:
            ROLLBACKS.labels(result="failed").inc()
            log("rollback_failed", action_id=action_id, error=str(exc))
            return store.transition(
                action_id,
                to=S.ROLLBACK_FAILED,
                actor=actor,
                event="rollback_failed",
                expected=[S.ROLLING_BACK],
                changes={"rollback": {**rollback_info, "error": f"{type(exc).__name__}: {exc}"}},
            )
    ROLLBACKS.labels(result="ok").inc()
    log("action_rolled_back", action_id=action_id, actor=actor)
    return store.transition(
        action_id,
        to=S.ROLLED_BACK,
        actor=actor,
        event="rolled_back",
        expected=[S.ROLLING_BACK],
        changes={
            "rollback": {
                **rollback_info,
                "completed_at": _now(),
                "state_before": before,
                "state_after": target.snapshot(),
                "result": result,
            }
        },
    )


# ----------------------------------------------------------------- expiry
def expire_stale(now: float | None = None) -> int:
    """Expire approval windows that have passed. Called by the worker."""
    now = now or _now()
    count = 0
    for action in store.list_actions(limit=1000):
        ch = action.get("challenge")
        window_closed = action["status"] in (S.PENDING_APPROVAL, S.ROLLBACK_PENDING) and ch and now >= ch["expires_at"]
        deferred = action.get("deferred_expires_at")
        capability_lapsed = action["status"] in (S.APPROVED, S.ROLLBACK_APPROVED) and deferred and now >= deferred
        if window_closed or capability_lapsed:
            try:
                store.transition(
                    action["id"],
                    to=S.EXPIRED,
                    actor="system",
                    event="expired",
                    expected=[action["status"]],
                    changes={"challenge": None},
                )
                count += 1
            except (InvalidTransition, ConflictError):
                continue
    return count


# ------------------------------------------------------------ public view
_PRIVATE_KEYS = {"challenge", "capability"}


def public_view(action: dict) -> dict:
    """The action as returned by the API: never includes challenges or capabilities."""
    view = {k: v for k, v in action.items() if k not in _PRIVATE_KEYS}
    view["approval_window_open"] = bool(action.get("challenge")) and not challenge_expired(action)
    return view
