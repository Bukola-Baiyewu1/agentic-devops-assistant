from fastapi.testclient import TestClient

from src.app import app
from src.state import store

client = TestClient(app)


def _alert(event_id="evt-1", name="High 5xx error rate", desc="500 errors after deploy"):
    return {"event_id": event_id, "name": name, "description": desc, "service": "web"}


def test_full_approve_flow_fixes_the_service():
    # 1) break the demo service
    client.post("/demo/break")
    assert client.get("/demo/health").json()["status"] == "unhealthy"

    # 2) alert -> the agent proposes a cited plan, pending approval
    r = client.post("/webhook/alert", json=_alert()).json()
    assert r["status"] == "pending_approval"
    assert r["proposed_tool"] == "restart_service"
    assert r["citation"] == "high-error-rate.md"          # grounded, not hallucinated
    action_id = r["action_id"]

    # 3) approve with the token -> the fix actually runs
    token = store.get_action(action_id)["approval_token"]
    ok = client.post(f"/actions/{action_id}/approve", json={"token": token}).json()
    assert ok["status"] == "executed"
    assert client.get("/demo/health").json()["status"] == "healthy"

    # 4) rollback restores the previous (broken) state
    rb = client.post(f"/actions/{action_id}/rollback").json()
    assert rb["status"] == "rolled_back"
    assert client.get("/demo/health").json()["status"] == "unhealthy"


def test_idempotency_same_event_id_makes_one_action():
    first = client.post("/webhook/alert", json=_alert("dup")).json()
    second = client.post("/webhook/alert", json=_alert("dup")).json()
    assert second["duplicate"] is True
    assert first["action_id"] == second["action_id"]


def test_bad_token_is_rejected():
    client.post("/demo/break")
    action_id = client.post("/webhook/alert", json=_alert("evt-bad")).json()["action_id"]
    r = client.post(f"/actions/{action_id}/approve", json={"token": "wrong"})
    assert r.status_code == 403


def test_disk_alert_escalates_without_action():
    r = client.post("/webhook/alert", json=_alert("evt-disk", "Disk pressure", "disk usage 91%")).json()
    assert r["status"] == "escalated"
    assert r["proposed_tool"] is None


def test_traces_are_recorded():
    client.post("/webhook/alert", json=_alert("evt-trace"))
    traces = client.get("/traces").json()
    assert len(traces) >= 1
    assert "cost_usd" in traces[0] and "latency_ms" in traces[0]
