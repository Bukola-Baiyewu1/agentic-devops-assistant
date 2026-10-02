import pytest

from src import tools
from src.demo import sim


def test_action_without_token_is_refused():
    with pytest.raises(tools.ApprovalError):
        tools.restart_service("web", approval_token="not-a-real-token")


def test_action_with_valid_token_runs_and_token_is_single_use():
    sim.inject_error()
    assert sim.health()["status"] == "unhealthy"

    tools.issue_token("tok123")
    out = tools.restart_service("web", approval_token="tok123")
    assert out["result"]["status"] == "healthy"

    # token cannot be reused
    with pytest.raises(tools.ApprovalError):
        tools.restart_service("web", approval_token="tok123")


def test_diagnostic_allowlist():
    assert "ok" in tools.run_diagnostic("uptime")
    assert "refused" in tools.run_diagnostic("rm -rf /")
