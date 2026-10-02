"""The action lifecycle as an explicit state machine.

Every status change in the system goes through `assert_transition`, and the
database applies it with a compare-and-set update (see state.Store.transition)
so two concurrent requests can never both move an action out of the same state.
"""

from __future__ import annotations

from enum import StrEnum


class ActionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    ESCALATED = "escalated"
    DENIED = "denied"
    EXPIRED = "expired"
    APPROVED = "approved"  # approved, execution deferred to an MCP client
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLBACK_APPROVED = "rollback_approved"  # approved, execution deferred to an MCP client
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_FAILED = "rollback_failed"


S = ActionStatus

ALLOWED_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    S.PENDING_APPROVAL: frozenset({S.EXECUTING, S.APPROVED, S.DENIED, S.EXPIRED}),
    S.APPROVED: frozenset({S.EXECUTING, S.EXPIRED}),
    S.EXECUTING: frozenset({S.EXECUTED, S.FAILED}),
    S.EXECUTED: frozenset({S.ROLLBACK_PENDING}),
    S.ROLLBACK_PENDING: frozenset({S.ROLLING_BACK, S.ROLLBACK_APPROVED, S.EXPIRED}),
    S.ROLLBACK_APPROVED: frozenset({S.ROLLING_BACK, S.EXPIRED}),
    S.ROLLING_BACK: frozenset({S.ROLLED_BACK, S.ROLLBACK_FAILED}),
    # terminal states
    S.ESCALATED: frozenset(),
    S.DENIED: frozenset(),
    S.EXPIRED: frozenset(),
    S.FAILED: frozenset(),
    S.ROLLED_BACK: frozenset(),
    S.ROLLBACK_FAILED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in ALLOWED_TRANSITIONS.items() if not nxt)


class InvalidTransition(ValueError):
    def __init__(self, current: str, target: str):
        super().__init__(f"cannot move action from '{current}' to '{target}'")
        self.current = current
        self.target = target


def can_transition(current: str, target: str) -> bool:
    try:
        return ActionStatus(target) in ALLOWED_TRANSITIONS[ActionStatus(current)]
    except ValueError:
        return False


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


class EventStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    RETRY_WAIT = "retry_wait"
    COMPLETED = "completed"
    DEAD_LETTERED = "dead_lettered"
