"""Stage 9: MCP is the tool interface; the approval workflow stays the authority.

Uses FastMCP's in-memory client, so these are real MCP protocol calls.
"""

import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from src.demo import sim
from src.mcp_server import mcp
from src.state import capabilities, store
from tests.conftest import challenge, make_alert


def call(name, args):
    async def go():
        async with Client(mcp) as c:
            result = await c.call_tool(name, args)
            return result.data if result.data is not None else result.structured_content

    return asyncio.run(go())


def list_tool_names():
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    return asyncio.run(go())


def approve_deferred(client, alert=None):
    """Human approves with execute=false; returns (action_id, capability)."""
    client.post("/demo/break", json={})
    action_id = client.post("/webhook/alert", json=alert or make_alert()).json()["action_id"]
    r = client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id), "execute": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved"
    return action_id, body["capability"]


def test_lists_read_and_action_tools():
    assert list_tool_names() == {
        "get_service_health",
        "get_recent_logs",
        "search_runbooks",
        "run_diagnostic",
        "get_action",
        "restart_service",
        "scale_service",
        "rollback",
    }


def test_read_tools_work_without_approval():
    sim.inject_error()
    assert call("get_service_health", {"service": "web"})["status"] == "unhealthy"
    assert "500 Internal Server Error" in call("get_recent_logs", {"service": "web", "lines": 5})
    hits = call("search_runbooks", {"query": "5xx errors after deploy"})
    assert hits[0]["chunk_id"].startswith("high-error-rate#")
    assert "refused" in call("run_diagnostic", {"command": "rm -rf /"})


def test_read_tools_refuse_unmanaged_services():
    with pytest.raises(ToolError):
        call("get_service_health", {"service": "payments"})


def test_action_tool_without_capability_fails(client):
    sim.inject_error()
    action_id = client.post("/webhook/alert", json=make_alert()).json()["action_id"]
    with pytest.raises(ToolError, match="refused"):
        call("restart_service", {"service": "web", "action_id": action_id, "capability": "cap_fake"})
    assert sim.health()["status"] == "unhealthy"
    assert store.get_action(action_id)["status"] == "pending_approval"


def test_exact_approved_action_succeeds_once_then_replay_fails(client):
    action_id, cap = approve_deferred(client)
    out = call("restart_service", {"service": "web", "action_id": action_id, "capability": cap})
    assert out["status"] == "executed"
    assert sim.health()["status"] == "healthy"
    sim.inject_error()
    with pytest.raises(ToolError, match="refused"):
        call("restart_service", {"service": "web", "action_id": action_id, "capability": cap})
    assert sim.health()["status"] == "unhealthy"


def test_wrong_scope_capability_fails(client):
    action_id, cap = approve_deferred(client)
    with pytest.raises(ToolError, match="refused"):
        call("rollback", {"action_id": action_id, "capability": cap})
    assert store.get_action(action_id)["status"] == "approved"


def test_changed_arguments_after_approval_fail(client):
    sim.overload()
    alert = make_alert("evt-cpu", "CPU saturation", "cpu at 95% sustained")
    action_id = client.post("/webhook/alert", json=alert).json()["action_id"]
    assert store.get_action(action_id)["tool_args"] == {"service": "web", "replicas": 2}
    cap = client.post(
        f"/actions/{action_id}/approve", json={"token": challenge(client, action_id), "execute": False}
    ).json()["capability"]
    with pytest.raises(ToolError, match="refused"):
        call("scale_service", {"service": "web", "replicas": 9, "action_id": action_id, "capability": cap})
    with pytest.raises(ToolError, match="refused"):
        call("restart_service", {"service": "web", "action_id": action_id, "capability": cap})
    # the approved action is untouched and the exact call still works
    assert store.get_action(action_id)["status"] == "approved"
    out = call("scale_service", {"service": "web", "replicas": 2, "action_id": action_id, "capability": cap})
    assert out["status"] == "executed" and sim.health()["replicas"] == 2


def test_capability_for_another_action_fails(client):
    first, cap = approve_deferred(client, make_alert("evt-1"))
    second, _ = approve_deferred(client, make_alert("evt-2"))
    with pytest.raises(ToolError, match="refused"):
        call("restart_service", {"service": "web", "action_id": second, "capability": cap})


def test_expired_capability_fails(client):
    action_id, cap = approve_deferred(client)
    with store.tx() as c:
        c.execute(capabilities.update().values(expires_at=0))
    with pytest.raises(ToolError, match="expired"):
        call("restart_service", {"service": "web", "action_id": action_id, "capability": cap})
    assert sim.health()["status"] == "unhealthy"


def test_rollback_through_mcp_needs_its_own_approval(client):
    action_id, cap = approve_deferred(client)
    call("restart_service", {"service": "web", "action_id": action_id, "capability": cap})
    client.post(f"/actions/{action_id}/rollback", json={})
    rb = client.post(
        f"/actions/{action_id}/rollback/approve", json={"token": challenge(client, action_id), "execute": False}
    ).json()
    assert rb["status"] == "rollback_approved"
    out = call("rollback", {"action_id": action_id, "capability": rb["capability"]})
    assert out["status"] == "rolled_back"
    assert sim.health()["status"] == "unhealthy"


def test_get_action_never_exposes_secrets(client):
    action_id, cap = approve_deferred(client)
    view = call("get_action", {"action_id": action_id})
    assert cap not in str(view) and "challenge" not in view and "capability" not in view
