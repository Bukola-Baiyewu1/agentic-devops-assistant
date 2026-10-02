"""Human-in-the-loop approval + execution + rollback.

An action created by the webhook is 'pending_approval'. Nothing runs until a
human approves it here, which is the only place tokens are handed to the tools.
Every executed action records the prior state so it can be rolled back.
"""
import uuid

from . import tools
from .demo import sim
from .state import store
from .observability import log


def create_pending_action(alert: dict, result: dict) -> dict:
    proposal = result["proposal"]
    escalated = proposal["needs_human"] or not proposal["tool_name"]
    token = None if escalated else uuid.uuid4().hex
    record = {
        "alert": alert,
        "proposal": proposal,
        "trace": result["trace"],
        "status": "escalated" if escalated else "pending_approval",
        "approval_token": token,
        "prior_state": None,
        "result": None,
    }
    action_id = store.create_action(record)
    store.add_trace({**result["trace"], "action_id": action_id})
    log("action_created", action_id=action_id, status=record["status"])
    return store.get_action(action_id)


def approve(action_id: str) -> dict:
    action = store.get_action(action_id)
    if not action:
        raise KeyError("action not found")
    if action["status"] != "pending_approval":
        raise ValueError(f"action is '{action['status']}', not pending_approval")

    proposal = action["proposal"]
    token = action["approval_token"]
    tools.issue_token(token)                      # the ONLY place a token is issued to tools

    prior = sim.snapshot()                         # capture state for rollback
    fn = tools.ACTION_TOOLS[proposal["tool_name"]]
    result = fn(**proposal["tool_args"], approval_token=token)

    store.update_action(action_id, status="executed", prior_state=prior, result=result)
    log("action_approved", action_id=action_id, tool=proposal["tool_name"])
    return store.get_action(action_id)


def deny(action_id: str) -> dict:
    action = store.get_action(action_id)
    if not action:
        raise KeyError("action not found")
    store.update_action(action_id, status="denied", approval_token=None)
    log("action_denied", action_id=action_id)
    return store.get_action(action_id)


def rollback(action_id: str) -> dict:
    action = store.get_action(action_id)
    if not action:
        raise KeyError("action not found")
    if action["status"] != "executed" or not action["prior_state"]:
        raise ValueError("nothing to roll back")
    sim.restore(action["prior_state"])
    store.update_action(action_id, status="rolled_back")
    log("action_rolled_back", action_id=action_id)
    return store.get_action(action_id)
