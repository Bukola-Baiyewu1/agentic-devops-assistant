"""Durable persistence for events, actions, audit records, traces, approval
capabilities, and the demo service state.

SQLAlchemy Core is used so the same code runs on SQLite (local development and
tests) and PostgreSQL (Docker Compose and deployment). Two properties matter
for safety and are enforced here, in the database, not only in Python:

* `events.event_id` is a primary key, so a duplicate alert cannot create a
  second row even when two requests race.
* Action status changes use optimistic locking (`version` column). If two
  approvals arrive at the same time, only one UPDATE matches and the other is
  rejected, so an action can never execute twice.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    delete,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement

from .config import settings
from .states import ActionStatus, InvalidTransition, assert_transition

metadata = MetaData()

events = Table(
    "events",
    metadata,
    Column("event_id", String(200), primary_key=True),
    Column("source", String(100), nullable=False, default="webhook"),
    Column("payload", JSON, nullable=False),
    Column("payload_hash", String(64), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_attempt_at", Float, nullable=True, index=True),
    Column("last_error", Text, nullable=True),
    Column("trace_id", String(64), nullable=True),
    Column("action_id", String(64), nullable=True),
    Column("received_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("locked_at", Float, nullable=True),
)

actions = Table(
    "actions",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("event_id", String(200), nullable=True, index=True),
    Column("service", String(64), nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("data", JSON, nullable=False),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("action_id", String(64), nullable=False, index=True),
    Column("at", Float, nullable=False),
    Column("actor", String(100), nullable=False),
    Column("event", String(64), nullable=False),
    Column("from_status", String(32), nullable=True),
    Column("to_status", String(32), nullable=True),
    Column("details", JSON, nullable=True),
)

traces = Table(
    "traces",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", Float, nullable=False),
    Column("action_id", String(64), nullable=True, index=True),
    Column("event_id", String(200), nullable=True),
    Column("data", JSON, nullable=False),
)

capabilities = Table(
    "capabilities",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("action_id", String(64), nullable=False, index=True),
    Column("scope", String(32), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("args_hash", String(64), nullable=False),
    Column("service", String(64), nullable=False),
    Column("approver", String(100), nullable=False),
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("used_at", Float, nullable=True),
)

kv = Table(
    "kv",
    metadata,
    Column("key", String(100), primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
    return create_engine(url, pool_pre_ping=True)


class ConflictError(RuntimeError):
    """Another writer changed the record first (optimistic-lock failure)."""


class Store:
    def __init__(self, url: str):
        self.url = url
        self.engine = _make_engine(url)
        metadata.create_all(self.engine)

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        with self.engine.begin() as conn:
            yield conn

    def reset(self) -> None:
        """Delete every row. Used by tests and the demo reset endpoint."""
        with self.tx() as c:
            for table in (audit_log, traces, capabilities, actions, events, kv):
                c.execute(delete(table))

    # ------------------------------------------------------------------ events
    def insert_event(
        self, event_id: str, payload: dict, payload_hash: str, trace_id: str, source: str = "webhook"
    ) -> bool:
        """Durably record an event. Returns False if the event_id already exists."""
        now = time.time()
        try:
            with self.tx() as c:
                c.execute(
                    insert(events).values(
                        event_id=event_id,
                        source=source,
                        payload=payload,
                        payload_hash=payload_hash,
                        status="queued",
                        attempts=0,
                        next_attempt_at=now,
                        trace_id=trace_id,
                        received_at=now,
                        updated_at=now,
                    )
                )
            return True
        except IntegrityError:
            return False

    def get_event(self, event_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(events).where(events.c.event_id == event_id)).mappings().first()
            return dict(row) if row else None

    def list_events(self, status: str | None = None, limit: int = 200) -> list[dict]:
        q = select(events).order_by(events.c.received_at.desc()).limit(limit)
        if status:
            q = q.where(events.c.status == status)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def claim_event(self, event_id: str, from_statuses: Iterable[str], stale_before: float | None = None) -> bool:
        """Atomically move an event to `processing`. Only one claimer can win.

        With `stale_before`, an event stuck in `processing` (its worker died)
        since before that time can also be reclaimed.
        """
        now = time.time()
        cond: ColumnElement[bool] = events.c.status.in_(list(from_statuses))
        if stale_before is not None:
            cond = or_(cond, and_(events.c.status == "processing", events.c.locked_at < stale_before))
        with self.tx() as c:
            res = c.execute(
                update(events)
                .where(events.c.event_id == event_id, cond)
                .values(status="processing", locked_at=now, updated_at=now)
            )
            return res.rowcount == 1

    def due_event_ids(self, now: float, stale_after: float, limit: int = 10) -> list[str]:
        with self.engine.connect() as c:
            due = (
                c.execute(
                    select(events.c.event_id)
                    .where(
                        events.c.status.in_(["queued", "retry_wait"]),
                        events.c.next_attempt_at <= now,
                    )
                    .order_by(events.c.next_attempt_at)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            stale = (
                c.execute(
                    select(events.c.event_id)
                    .where(events.c.status == "processing", events.c.locked_at < now - stale_after)
                    .limit(limit)
                )
                .scalars()
                .all()
            )
        return list(due) + list(stale)

    def update_event(self, event_id: str, **values: Any) -> None:
        values["updated_at"] = time.time()
        with self.tx() as c:
            c.execute(update(events).where(events.c.event_id == event_id).values(**values))

    # ----------------------------------------------------------------- actions
    @staticmethod
    def _row_to_action(row: Any) -> dict:
        data = dict(row["data"] or {})
        data.update(
            id=row["id"],
            event_id=row["event_id"],
            service=row["service"],
            status=row["status"],
            version=row["version"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        return data

    def create_action(self, *, service: str, status: str, event_id: str | None, data: dict, actor: str) -> dict:
        action_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.tx() as c:
            c.execute(
                insert(actions).values(
                    id=action_id,
                    event_id=event_id,
                    service=service,
                    status=status,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    data=data,
                )
            )
            c.execute(
                insert(audit_log).values(
                    action_id=action_id,
                    at=now,
                    actor=actor,
                    event="created",
                    from_status=None,
                    to_status=status,
                    details=None,
                )
            )
        action = self.get_action(action_id)
        assert action is not None
        return action

    def get_action(self, action_id: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(actions).where(actions.c.id == action_id)).mappings().first()
            return self._row_to_action(row) if row else None

    def list_actions(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(actions).order_by(actions.c.created_at.desc()).limit(limit)).mappings()
            return [self._row_to_action(r) for r in rows]

    def transition(
        self,
        action_id: str,
        *,
        to: str,
        actor: str,
        event: str,
        expected: Iterable[str] | None = None,
        changes: dict | None = None,
        details: dict | None = None,
    ) -> dict:
        """Change an action's status with state-machine and optimistic-lock checks.

        Raises KeyError (missing), InvalidTransition (illegal move or the
        current status is not one of `expected`) or ConflictError (a concurrent
        writer won the race).
        """
        current = self.get_action(action_id)
        if current is None:
            raise KeyError("action not found")
        cur_status = current["status"]
        if expected is not None and cur_status not in set(expected):
            raise InvalidTransition(cur_status, to)
        assert_transition(cur_status, to)
        return self._write(current, to=to, actor=actor, event=event, changes=changes, details=details)

    def update_action(
        self, action_id: str, *, actor: str, event: str, changes: dict, details: dict | None = None
    ) -> dict:
        """Change fields without changing status (still optimistic-locked + audited)."""
        current = self.get_action(action_id)
        if current is None:
            raise KeyError("action not found")
        return self._write(current, to=None, actor=actor, event=event, changes=changes, details=details)

    def _write(
        self, current: dict, *, to: str | None, actor: str, event: str, changes: dict | None, details: dict | None
    ) -> dict:
        reserved = {"id", "event_id", "service", "status", "version", "created_at", "updated_at"}
        data = {k: v for k, v in current.items() if k not in reserved}
        data.update(changes or {})
        now = time.time()
        new_status = to or current["status"]
        with self.tx() as c:
            res = c.execute(
                update(actions)
                .where(actions.c.id == current["id"], actions.c.version == current["version"])
                .values(status=new_status, version=current["version"] + 1, updated_at=now, data=data)
            )
            if res.rowcount != 1:
                raise ConflictError("action was modified concurrently")
            c.execute(
                insert(audit_log).values(
                    action_id=current["id"],
                    at=now,
                    actor=actor,
                    event=event,
                    from_status=current["status"],
                    to_status=new_status,
                    details=details,
                )
            )
        updated = self.get_action(current["id"])
        assert updated is not None
        return updated

    def list_audit(self, action_id: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(
                select(audit_log).where(audit_log.c.action_id == action_id).order_by(audit_log.c.id)
            ).mappings()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------------ traces
    def add_trace(self, data: dict, action_id: str | None = None, event_id: str | None = None) -> None:
        with self.tx() as c:
            c.execute(insert(traces).values(at=time.time(), action_id=action_id, event_id=event_id, data=data))

    def list_traces(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(traces).order_by(traces.c.id.desc()).limit(limit)).mappings()
            return [
                {**dict(r["data"]), "action_id": r["action_id"], "event_id": r["event_id"], "at": r["at"]} for r in rows
            ]

    # ------------------------------------------------------------ capabilities
    def insert_capability(self, **values: Any) -> None:
        with self.tx() as c:
            c.execute(insert(capabilities).values(**values))

    def get_capability(self, token_hash: str) -> dict | None:
        with self.engine.connect() as c:
            row = c.execute(select(capabilities).where(capabilities.c.token_hash == token_hash)).mappings().first()
            return dict(row) if row else None

    def mark_capability_used(self, token_hash: str, now: float) -> bool:
        """Atomically spend a capability. Returns False if already used or expired."""
        with self.tx() as c:
            res = c.execute(
                update(capabilities)
                .where(
                    capabilities.c.token_hash == token_hash,
                    capabilities.c.used_at.is_(None),
                    capabilities.c.expires_at > now,
                )
                .values(used_at=now)
            )
            return res.rowcount == 1

    # ---------------------------------------------------------------------- kv
    def get_kv(self, key: str) -> Any:
        with self.engine.connect() as c:
            return c.execute(select(kv.c.value).where(kv.c.key == key)).scalar()

    def set_kv(self, key: str, value: Any) -> None:
        now = time.time()
        with self.tx() as c:
            res = c.execute(update(kv).where(kv.c.key == key).values(value=value, updated_at=now))
            if res.rowcount == 0:
                try:
                    with c.begin_nested():
                        c.execute(insert(kv).values(key=key, value=value, updated_at=now))
                except IntegrityError:
                    c.execute(update(kv).where(kv.c.key == key).values(value=value, updated_at=now))


store = Store(settings.database_url)

__all__ = ["Store", "store", "ConflictError", "InvalidTransition", "ActionStatus"]
