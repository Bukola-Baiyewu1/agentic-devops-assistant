import pytest

from src.states import ALLOWED_TRANSITIONS, TERMINAL, InvalidTransition, assert_transition, can_transition
from src.states import ActionStatus as S


@pytest.mark.parametrize(
    "current,target",
    [
        (S.PENDING_APPROVAL, S.EXECUTING),
        (S.PENDING_APPROVAL, S.DENIED),
        (S.EXECUTING, S.EXECUTED),
        (S.EXECUTING, S.FAILED),
        (S.EXECUTED, S.ROLLBACK_PENDING),
        (S.ROLLBACK_PENDING, S.ROLLING_BACK),
        (S.ROLLING_BACK, S.ROLLED_BACK),
    ],
)
def test_guide_transitions_are_allowed(current, target):
    assert can_transition(current, target)


@pytest.mark.parametrize(
    "current,target",
    [
        (S.DENIED, S.EXECUTING),
        (S.EXECUTED, S.DENIED),
        (S.EXECUTED, S.EXECUTING),
        (S.PENDING_APPROVAL, S.EXECUTED),  # must pass through executing
        (S.PENDING_APPROVAL, S.ROLLED_BACK),
        (S.ROLLED_BACK, S.EXECUTED),
        (S.ESCALATED, S.EXECUTING),
        (S.FAILED, S.EXECUTED),
    ],
)
def test_everything_else_is_rejected(current, target):
    assert not can_transition(current, target)
    with pytest.raises(InvalidTransition):
        assert_transition(current, target)


def test_terminal_states_have_no_exits():
    for s in TERMINAL:
        assert ALLOWED_TRANSITIONS[s] == frozenset()
    assert {S.DENIED, S.EXPIRED, S.FAILED, S.ROLLED_BACK, S.ESCALATED, S.ROLLBACK_FAILED} == set(TERMINAL)


def test_unknown_status_is_rejected():
    assert not can_transition("banana", "executed")
