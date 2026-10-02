"""Stage 6: the approval boundary, state transitions, token hygiene, and rollback."""

import threading

import pytest

from src import approval
from src.demo import sim
from src.state import store
from tests.conftest import BOB, broken_alert, challenge, make_alert


def health(client):
    return client.get("/demo/health").json()["status"]


# ------------------------------------------------------------ happy path
def test_full_flow_break_alert_approve_rollback(client):
    client.post("/demo/break", json={})
    assert health(client) == "unhealthy"

    r = client.post("/webhook/alert", json=make_alert()).json()
    assert r["status"] == "pending_approval"
    assert r["proposed_tool"] == "restart_service"
    assert r["citation"] == "high-error-rate.md"
    assert r["citation_detail"]["chunk_id"] == "high-error-rate#restart-after-a-recent-deploy"
    action_id = r["action_id"]
    assert health(client) == "unhealthy", "nothing may run before approval"

    done = client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)}).json()
    assert done["status"] == "executed"
    assert health(client) == "healthy"

    client.post(f"/actions/{action_id}/rollback", json={})
    rb = client.post(f"/actions/{action_id}/rollback/approve", json={"token": challenge(client, action_id)}).json()
    assert rb["status"] == "rolled_back"
    assert health(client) == "unhealthy"


def test_audit_record_has_approver_timestamps_and_states(client):
    action_id = broken_alert(client)
    client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)})
    a = client.get(f"/actions/{action_id}").json()
    assert a["approver"] == "alice"
    assert a["decision"] == "approved"
    assert a["requested_at"] <= a["decided_at"] <= a["executed_at"]
    assert a["prior_state"]["broken"] is True
    assert a["new_state"]["broken"] is False
    assert a["tool_args"] == {"service": "web"}
    assert a["citation"]["lines"] == "10-14"
    events = [e["event"] for e in client.get(f"/actions/{action_id}/audit").json()]
    assert events == ["created", "approved", "executed"]


def test_hashed_password_user_can_approve(client, anon):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    r = anon.post(f"/actions/{action_id}/approve", json={"token": token}, auth=BOB)
    assert r.status_code == 200
    assert r.json()["approver"] == "bob"


# ------------------------------------------------------------ Stage 6A
def test_missing_approval_token_is_rejected(client):
    action_id = broken_alert(client, "evt-missing-token")
    response = client.post(f"/actions/{action_id}/approve", json={})
    assert response.status_code in (403, 422)
    assert store.get_action(action_id)["status"] == "pending_approval"
    assert health(client) == "unhealthy"


def test_empty_approval_token_is_rejected(client):
    action_id = broken_alert(client, "evt-empty-token")
    response = client.post(f"/actions/{action_id}/approve", json={"token": ""})
    assert response.status_code in (403, 422)
    assert store.get_action(action_id)["status"] == "pending_approval"


def test_wrong_approval_token_is_rejected_without_execution(client):
    action_id = broken_alert(client, "evt-wrong-token-2")
    response = client.post(f"/actions/{action_id}/approve", json={"token": "definitely-wrong"})
    assert response.status_code == 403
    assert store.get_action(action_id)["status"] == "pending_approval"
    assert health(client) == "unhealthy"


def test_unauthenticated_user_cannot_approve(client, anon):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    r = anon.post(f"/actions/{action_id}/approve", json={"token": token})
    assert r.status_code == 401
    r = anon.post(f"/actions/{action_id}/approve", json={"token": token}, auth=("alice", "wrong"))
    assert r.status_code == 401
    assert store.get_action(action_id)["status"] == "pending_approval"


def test_token_for_one_action_cannot_approve_another(client):
    first = broken_alert(client, "evt-a")
    second = broken_alert(client, "evt-b")
    r = client.post(f"/actions/{second}/approve", json={"token": challenge(client, first)})
    assert r.status_code == 403
    assert store.get_action(second)["status"] == "pending_approval"


# ------------------------------------------------------------ Stage 6B
def test_denied_action_cannot_be_approved(client):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    assert client.post(f"/actions/{action_id}/deny", json={"token": token}).json()["status"] == "denied"
    r = client.post(f"/actions/{action_id}/approve", json={"token": token})
    assert r.status_code == 409
    assert store.get_action(action_id)["status"] == "denied"
    assert health(client) == "unhealthy"


def test_executed_action_cannot_be_denied(client):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    client.post(f"/actions/{action_id}/approve", json={"token": token})
    r = client.post(f"/actions/{action_id}/deny", json={"token": token})
    assert r.status_code == 409
    assert store.get_action(action_id)["status"] == "executed"


def test_token_cannot_be_replayed(client):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    assert client.post(f"/actions/{action_id}/approve", json={"token": token}).status_code == 200
    client.post("/demo/break", json={})
    r = client.post(f"/actions/{action_id}/approve", json={"token": token})
    assert r.status_code == 409
    assert health(client) == "unhealthy", "a replayed token must not execute again"


def test_failed_execution_has_failed_status(client, monkeypatch):
    action_id = broken_alert(client)

    def boom(self):
        raise RuntimeError("simulated tool crash")

    monkeypatch.setattr(type(sim), "restart", boom)
    r = client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)})
    assert r.status_code == 200
    a = r.json()
    assert a["status"] == "failed"
    assert "simulated tool crash" in a["error"]
    events = [e["to_status"] for e in client.get(f"/actions/{action_id}/audit").json()]
    assert events == ["pending_approval", "executing", "failed"]


def test_escalated_action_cannot_be_approved(client):
    r = client.post("/webhook/alert", json=make_alert("evt-disk", "Disk pressure", "disk usage 91%")).json()
    assert r["status"] == "escalated"
    assert r["proposed_tool"] is None
    assert client.get(f"/actions/{r['action_id']}/challenge").status_code == 409


def test_concurrent_approvals_execute_exactly_once(client, monkeypatch):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    calls = []
    original = type(sim).restart

    def counting_restart(self):
        calls.append(1)
        original(self)

    monkeypatch.setattr(type(sim), "restart", counting_restart)
    results, barrier = [], threading.Barrier(5)

    def worker():
        barrier.wait()
        try:
            results.append(approval.approve(action_id, token=token, approver="alice")["status"])
        except Exception as exc:  # the losers must fail, not execute
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("executed") == 1, results
    assert len(calls) == 1
    assert store.get_action(action_id)["status"] == "executed"


def test_expired_approval_window_is_refused(client):
    action_id = broken_alert(client)
    token = challenge(client, action_id)
    action = store.get_action(action_id)
    store.update_action(
        action_id, actor="test", event="test", changes={"challenge": {**action["challenge"], "expires_at": 0}}
    )
    r = client.post(f"/actions/{action_id}/approve", json={"token": token})
    assert r.status_code == 410
    assert store.get_action(action_id)["status"] == "expired"
    assert health(client) == "unhealthy"


def test_worker_expires_stale_approvals(client):
    action_id = broken_alert(client)
    action = store.get_action(action_id)
    store.update_action(
        action_id, actor="test", event="test", changes={"challenge": {**action["challenge"], "expires_at": 0}}
    )
    assert approval.expire_stale() == 1
    assert store.get_action(action_id)["status"] == "expired"


# ------------------------------------------------------------ Stage 6C
def _contains_secret(obj, secrets):
    text = str(obj)
    return any(s and s in text for s in secrets)


def test_action_endpoints_do_not_return_approval_token(client):
    client.post("/demo/break", json={})
    webhook = client.post("/webhook/alert", json=make_alert()).json()
    action_id = webhook["action_id"]
    token = challenge(client, action_id)
    nonce = store.get_action(action_id)["challenge"]["nonce"]
    for body in (webhook, client.get("/actions").json(), client.get(f"/actions/{action_id}").json()):
        assert not _contains_secret(body, [token, nonce])
        assert "approval_token" not in str(body)
        assert "challenge" not in (body if isinstance(body, dict) else body[0])


def test_approval_page_escapes_untrusted_alert_text(client):
    client.post("/demo/break", json={})
    evil = "<script>alert('x')</script> 500 errors"
    r = client.post("/webhook/alert", json=make_alert("evt-xss", name=evil, desc=evil)).json()
    page = client.get(f"/approve/{r['action_id']}")
    assert page.status_code == 200
    assert "<script>alert('x')</script>" not in page.text
    assert "&lt;script&gt;" in page.text
    assert "script-src 'nonce-" in page.headers["content-security-policy"]


def test_approval_page_requires_login(client, anon):
    action_id = broken_alert(client)
    assert anon.get(f"/approve/{action_id}").status_code == 401


def test_rollback_request_does_not_immediately_change_service(client):
    action_id = broken_alert(client)
    client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)})
    assert health(client) == "healthy"
    r = client.post(f"/actions/{action_id}/rollback", json={})
    assert r.json()["status"] == "rollback_pending"
    assert health(client) == "healthy"


def test_rollback_requires_a_fresh_token(client):
    action_id = broken_alert(client)
    client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)})
    client.post(f"/actions/{action_id}/rollback", json={})
    r = client.post(f"/actions/{action_id}/rollback/approve", json={})
    assert r.status_code == 422
    r = client.post(f"/actions/{action_id}/rollback/approve", json={"token": "made-up"})
    assert r.status_code == 403
    assert store.get_action(action_id)["status"] == "rollback_pending"


def test_original_execution_token_cannot_approve_rollback(client):
    action_id = broken_alert(client)
    original = challenge(client, action_id)
    client.post(f"/actions/{action_id}/approve", json={"token": original})
    client.post(f"/actions/{action_id}/rollback", json={})
    r = client.post(f"/actions/{action_id}/rollback/approve", json={"token": original})
    assert r.status_code == 403
    assert health(client) == "healthy"


def test_rollback_token_is_single_use(client):
    action_id = broken_alert(client)
    client.post(f"/actions/{action_id}/approve", json={"token": challenge(client, action_id)})
    client.post(f"/actions/{action_id}/rollback", json={})
    rb_token = challenge(client, action_id)
    assert client.post(f"/actions/{action_id}/rollback/approve", json={"token": rb_token}).status_code == 200
    r = client.post(f"/actions/{action_id}/rollback/approve", json={"token": rb_token})
    assert r.status_code == 409


def test_rollback_only_from_executed(client):
    action_id = broken_alert(client)
    r = client.post(f"/actions/{action_id}/rollback", json={})
    assert r.status_code == 409


def test_post_without_json_content_type_is_refused(client):
    action_id = broken_alert(client)
    r = client.post(
        f"/actions/{action_id}/approve",
        content=b"token=x",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 415


@pytest.mark.parametrize("path", ["/actions", "/traces", "/events"])
def test_private_endpoints_require_login(anon, path):
    assert anon.get(path).status_code == 401
