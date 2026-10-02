"""Action tools refuse to run without an exact, unexpired, unused capability."""

import time

import pytest

from src import security, tools
from src.demo import sim
from src.security import ApprovalError, mint_capability
from src.state import store


def cap(tool="restart_service", args=None, service="web", action_id="a1", scope="execute"):
    return mint_capability(
        action_id=action_id,
        scope=scope,
        tool=tool,
        args=args or {"service": service},
        service=service,
        approver="alice",
    )


def test_action_without_capability_is_refused():
    with pytest.raises(ApprovalError):
        tools.restart_service("web")
    with pytest.raises(ApprovalError):
        tools.restart_service("web", capability="not-a-real-capability")


def test_valid_capability_runs_once():
    sim.inject_error()
    token = cap()
    out = tools.restart_service("web", capability=token)
    assert out["result"]["status"] == "healthy"
    with pytest.raises(ApprovalError, match="already used"):
        tools.restart_service("web", capability=token)


def test_capability_is_bound_to_tool():
    token = cap(tool="restart_service")
    with pytest.raises(ApprovalError, match="tool"):
        tools.scale_service("web", 2, capability=token)


def test_capability_is_bound_to_arguments():
    token = cap(tool="scale_service", args={"service": "web", "replicas": 2})
    with pytest.raises(ApprovalError, match="arguments"):
        tools.scale_service("web", 5, capability=token)
    out = tools.scale_service("web", 2, capability=token)
    assert out["replicas"] == 2


def test_capability_is_bound_to_action():
    token = cap(action_id="a1")
    with pytest.raises(ApprovalError, match="action"):
        tools.restart_service("web", capability=token, action_id="other")


def test_execute_capability_cannot_roll_back():
    token = cap()
    with pytest.raises(ApprovalError, match="scope"):
        tools.rollback_service("web", action_id="a1", prior_state=sim.snapshot(), capability=token)


def test_expired_capability_is_refused():
    token = cap()
    with store.tx() as c:
        c.execute(store_capabilities().update().values(expires_at=time.time() - 1))
    with pytest.raises(ApprovalError, match="expired"):
        tools.restart_service("web", capability=token)


def store_capabilities():
    from src.state import capabilities

    return capabilities


def test_only_capability_hash_is_stored():
    token = cap()
    with store.engine.connect() as c:
        rows = c.execute(store_capabilities().select()).mappings().all()
    assert rows and all(token not in str(dict(r)) for r in rows)
    assert rows[0]["token_hash"] == security.hash_token(token)


def test_unknown_service_is_refused_even_for_reads():
    with pytest.raises(ValueError):
        tools.get_service_health("payments")
    with pytest.raises(ValueError):
        tools.get_service_health("../etc/passwd")


def test_diagnostic_allowlist():
    assert "ok" in tools.run_diagnostic("uptime")
    assert "refused" in tools.run_diagnostic("rm -rf /")
    assert "refused" in tools.run_diagnostic("df; cat /etc/shadow")
    assert "refused" in tools.run_diagnostic("ps aux")


def test_log_lines_are_bounded():
    with pytest.raises(ValueError):
        tools.get_recent_logs("web", 100000)
