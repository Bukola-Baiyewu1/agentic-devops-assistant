"""Stage 10: durable events, retries with backoff, dead letters, and replay."""

import random
import threading
import time

import pytest

from src import events, worker
from src.config import settings
from src.state import store
from tests.conftest import challenge, make_alert


def later():
    return time.time() + 10_000


def test_event_is_stored_before_response(client):
    r = client.post("/webhook/alert", json=make_alert("evt-store"))
    event = store.get_event("evt-store")
    assert event["status"] == "completed"
    assert event["action_id"] == r.json()["action_id"]
    assert event["payload"]["name"] == "High 5xx error rate"


def test_duplicate_event_creates_one_action(client):
    first = client.post("/webhook/alert", json=make_alert("dup")).json()
    second = client.post("/webhook/alert", json=make_alert("dup")).json()
    assert second["duplicate"] is True
    assert first["action_id"] == second["action_id"]
    assert len(store.list_actions()) == 1


def test_concurrent_duplicate_ingest_is_atomic():
    results, barrier = [], threading.Barrier(6)

    def go():
        barrier.wait()
        results.append(events.ingest(make_alert("race"), trace_id="t")[0])

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


def test_temporary_failure_retries_then_succeeds(client):
    client.post("/demo/break", json={})
    client.post("/demo/planner-faults", json={"count": 2, "kind": "transient"})
    r = client.post("/webhook/alert", json=make_alert("evt-retry"))
    assert r.status_code == 202
    assert r.json()["event_status"] == "retry_wait"

    event = store.get_event("evt-retry")
    assert event["attempts"] == 1 and "transient" in event["last_error"]
    assert event["next_attempt_at"] > time.time()
    assert events.run_due(now=time.time()) == 0, "not due yet: backoff must be respected"

    events.run_due(now=later())  # attempt 2 fails
    assert store.get_event("evt-retry")["status"] == "retry_wait"
    events.run_due(now=later())  # attempt 3 succeeds
    event = store.get_event("evt-retry")
    assert event["status"] == "completed" and event["attempts"] == 3
    action = store.get_action(event["action_id"])
    assert action["status"] == "pending_approval", "a retried event still needs approval"


def test_max_attempts_moves_event_to_dead_letter(client):
    settings.max_event_attempts = 3
    client.post("/demo/planner-faults", json={"count": 10, "kind": "transient"})
    client.post("/webhook/alert", json=make_alert("evt-dlq"))
    events.run_due(now=later())
    events.run_due(now=later())
    event = store.get_event("evt-dlq")
    assert event["status"] == "dead_lettered"
    assert event["attempts"] == 3
    assert event["action_id"] is None
    dead = client.get("/events", params={"status": "dead_lettered"}).json()
    assert [e["event_id"] for e in dead] == ["evt-dlq"]


def test_permanent_planner_failure_escalates_instead_of_retrying(client):
    client.post("/demo/planner-faults", json={"count": 1, "kind": "permanent"})
    r = client.post("/webhook/alert", json=make_alert("evt-perm")).json()
    assert r["event_status"] == "completed" and r["attempts"] == 1
    assert r["status"] == "escalated"


def test_unexpected_error_is_dead_lettered_immediately(client, monkeypatch):
    from src import agent

    def broken(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(agent, "plan", broken)
    r = client.post("/webhook/alert", json=make_alert("evt-bug"))
    assert r.status_code == 202
    event = store.get_event("evt-bug")
    assert event["status"] == "dead_lettered" and event["attempts"] == 1


def test_replay_dead_letter_goes_through_approval(client):
    settings.max_event_attempts = 1
    client.post("/demo/break", json={})
    client.post("/demo/planner-faults", json={"count": 1, "kind": "transient"})
    client.post("/webhook/alert", json=make_alert("evt-replay"))
    assert store.get_event("evt-replay")["status"] == "dead_lettered"

    replayed = client.post("/events/evt-replay/replay", json={}).json()
    assert replayed["status"] == "completed"
    action = store.get_action(replayed["action_id"])
    assert action["status"] == "pending_approval"
    assert client.get("/demo/health").json()["status"] == "unhealthy"
    # and approving it works normally
    done = client.post(f"/actions/{action['id']}/approve", json={"token": challenge(client, action["id"])}).json()
    assert done["status"] == "executed"


def test_replay_refuses_events_that_are_not_dead_lettered(client):
    client.post("/webhook/alert", json=make_alert("evt-done"))
    r = client.post("/events/evt-done/replay", json={})
    assert r.status_code == 400
    assert client.post("/events/nope/replay", json={}).status_code == 404


def test_queued_mode_returns_202_and_worker_processes(client):
    settings.process_inline = False
    r = client.post("/webhook/alert", json=make_alert("evt-queued"))
    assert r.status_code == 202 and r.json()["event_status"] == "queued"
    assert worker.run_once()["processed"] == 1
    assert store.get_event("evt-queued")["status"] == "completed"
    again = client.post("/webhook/alert", json=make_alert("evt-queued")).json()
    assert again["duplicate"] is True and again["status"] == "pending_approval"


def test_stale_processing_event_is_reclaimed():
    settings.process_inline = False
    events.ingest(make_alert("evt-stale"), trace_id="t")
    assert store.claim_event("evt-stale", ("queued",))
    store.update_event("evt-stale", locked_at=time.time() - 3600)
    events.run_due(now=time.time())
    assert store.get_event("evt-stale")["status"] == "completed"


def test_fresh_processing_event_is_not_double_claimed():
    events.ingest(make_alert("evt-busy"), trace_id="t")
    assert store.claim_event("evt-busy", ("queued",))
    assert events.run_due(now=time.time()) == 0
    assert not store.claim_event("evt-busy", ("queued", "retry_wait"))


@pytest.mark.parametrize("attempt", [1, 2, 3, 6, 20])
def test_backoff_is_exponential_with_jitter_and_capped(attempt):
    rng = random.Random(42)
    ceiling = min(settings.retry_max_seconds, settings.retry_base_seconds * 2 ** (attempt - 1))
    values = [events.backoff_seconds(attempt, rng) for _ in range(50)]
    assert all(ceiling / 2 <= v <= ceiling for v in values)
    assert len({round(v, 6) for v in values}) > 1, "jitter must vary the delay"
