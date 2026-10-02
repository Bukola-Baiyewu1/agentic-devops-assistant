"""Durable event lifecycle: receive -> queue -> process -> retry -> dead letter.

    received -> queued -> processing -> completed
                        -> retry_wait -> processing ...
                        -> dead_lettered  (operator can replay)

* An event is written to the database before the webhook answers, so an
  alert is never lost once it has been acknowledged.
* `event_id` is the primary key: duplicates are rejected by the database.
* Only temporary failures are retried, with exponential backoff and jitter,
  up to `AEGIS_MAX_EVENT_ATTEMPTS`. Anything else, or an exhausted event,
  is dead-lettered and visible at GET /events?status=dead_lettered.
* A replayed event goes through exactly the same planning and approval path.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Any

from sqlalchemy.exc import OperationalError

from . import agent, approval
from .config import settings
from .observability import (
    ALERTS,
    DEAD_LETTERS,
    DUPLICATES,
    RETRIES,
    log,
    reset_correlation_id,
    set_correlation_id,
)
from .planners import TransientPlannerError
from .state import store
from .states import EventStatus as E

RETRYABLE = (TransientPlannerError, OperationalError)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random."""
    gen: Any = rng if rng is not None else random
    ceiling = min(settings.retry_max_seconds, settings.retry_base_seconds * (2 ** max(0, attempt - 1)))
    return ceiling / 2 + gen.uniform(0, ceiling / 2)


def ingest(alert: dict, trace_id: str, source: str = "webhook") -> tuple[bool, dict]:
    """Durably record an alert. Returns (created, event)."""
    h = payload_hash(alert)
    created = store.insert_event(alert["event_id"], alert, h, trace_id, source=source)
    event = store.get_event(alert["event_id"])
    assert event is not None
    if not created:
        DUPLICATES.inc()
        log(
            "duplicate_event",
            event_id=alert["event_id"],
            action_id=event["action_id"],
            payload_changed=event["payload_hash"] != h,
        )
    else:
        ALERTS.labels(status="received").inc()
        log("event_received", event_id=alert["event_id"])
    return created, event


def process_event(event_id: str, stale_before: float | None = None) -> dict:
    """Claim and process one event. Safe to call concurrently: one claimer wins."""
    if not store.claim_event(event_id, (E.QUEUED, E.RETRY_WAIT), stale_before=stale_before):
        event = store.get_event(event_id)
        if event is None:
            raise KeyError("event not found")
        return event

    event = store.get_event(event_id)
    assert event is not None
    token = set_correlation_id(event["trace_id"] or event_id)
    attempts = int(event["attempts"]) + 1
    try:
        result = agent.plan(event["payload"])
        action = approval.create_pending_action(event["payload"], result, event_id=event_id)
        store.update_event(
            event_id, status=E.COMPLETED, attempts=attempts, action_id=action["id"], last_error=None, locked_at=None
        )
        ALERTS.labels(status=action["status"]).inc()
        log("event_completed", event_id=event_id, action_id=action["id"], attempts=attempts)
    except RETRYABLE as exc:
        error = f"{type(exc).__name__}: {exc}"
        if attempts >= settings.max_event_attempts:
            _dead_letter(event_id, attempts, error)
        else:
            delay = backoff_seconds(attempts)
            store.update_event(
                event_id,
                status=E.RETRY_WAIT,
                attempts=attempts,
                last_error=error,
                next_attempt_at=time.time() + delay,
                locked_at=None,
            )
            RETRIES.inc()
            ALERTS.labels(status="retry_wait").inc()
            log("event_retry_scheduled", event_id=event_id, attempts=attempts, delay_s=round(delay, 2), error=error)
    except Exception as exc:  # not a temporary failure: do not retry blindly
        _dead_letter(event_id, attempts, f"{type(exc).__name__}: {exc}")
    finally:
        reset_correlation_id(token)
    event = store.get_event(event_id)
    assert event is not None
    return event


def _dead_letter(event_id: str, attempts: int, error: str) -> None:
    store.update_event(
        event_id, status=E.DEAD_LETTERED, attempts=attempts, last_error=error, next_attempt_at=None, locked_at=None
    )
    DEAD_LETTERS.inc()
    ALERTS.labels(status="dead_lettered").inc()
    log("event_dead_lettered", event_id=event_id, attempts=attempts, error=error)


def replay(event_id: str, *, actor: str) -> dict:
    """Put a dead-lettered event back on the queue. Idempotency still applies:
    an event that already produced an action can never be replayed."""
    event = store.get_event(event_id)
    if event is None:
        raise KeyError("event not found")
    if event["status"] != E.DEAD_LETTERED:
        raise ValueError(f"only dead-lettered events can be replayed (status is '{event['status']}')")
    if event["action_id"]:
        raise ValueError("event already produced an action; replay refused")
    store.update_event(event_id, status=E.QUEUED, attempts=0, next_attempt_at=time.time())
    log("event_replayed", event_id=event_id, actor=actor)
    updated = store.get_event(event_id)
    assert updated is not None
    return updated


def run_due(now: float | None = None, limit: int = 10) -> int:
    """Process events that are due. Returns how many were attempted."""
    now = now or time.time()
    stale_after = max(60.0, settings.llm_timeout_seconds * settings.max_plan_iterations * 2)
    ids = store.due_event_ids(now, stale_after=stale_after, limit=limit)
    for event_id in ids:
        process_event(event_id, stale_before=now - stale_after)
    return len(ids)
