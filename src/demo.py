"""A simulated target service that Aegis monitors and repairs.

The state is stored in the database (not in process memory), so the API
process, the background worker, and the MCP server all observe and change the
same service. This is the ONLY infrastructure Aegis controls in this project:
it is a safe simulator, not a real cluster. A real adapter (Kubernetes,
Docker API proxy, cloud API) would implement the same small surface:
health / logs / snapshot / restore / restart / scale.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from .state import Store
from .state import store as default_store

_MAX_LOG_LINES = 200


def _default_state(name: str) -> dict:
    return {
        "name": name,
        "broken": False,
        "replicas": 1,
        "cpu": 30,
        "logs": ["[info] service started", "[info] listening on :8080"],
    }


class ServiceSimulator:
    def __init__(self, db: Store, name: str = "web"):
        self.db = db
        self.name = name
        self._lock = threading.Lock()

    # ---- persistence -----------------------------------------------------
    @property
    def _key(self) -> str:
        return f"sim:{self.name}"

    def _load(self) -> dict:
        state = self.db.get_kv(self._key)
        return dict(state) if state else _default_state(self.name)

    def _save(self, state: dict) -> None:
        state["logs"] = state["logs"][-_MAX_LOG_LINES:]
        self.db.set_kv(self._key, state)

    def _mutate(self, fn: Any) -> dict:
        with self._lock:
            state = self._load()
            fn(state)
            self._save(state)
            return state

    def reset(self) -> None:
        with self._lock:
            self._save(_default_state(self.name))

    # ---- fault injection -------------------------------------------------
    def inject_error(self) -> None:
        def f(s: dict) -> None:
            s["broken"] = True
            s["cpu"] = 92
            s["logs"].append("[error] 500 Internal Server Error (x37)")
            s["logs"].append("[error] unhandled exception in request handler")

        self._mutate(f)

    def clear_error(self) -> None:
        def f(s: dict) -> None:
            s["broken"] = False
            s["cpu"] = 30
            s["logs"].append("[info] service recovered, error rate back to normal")

        self._mutate(f)

    def overload(self) -> None:
        def f(s: dict) -> None:
            s["cpu"] = 95
            s["logs"].append("[warn] p95 latency 2400ms above 500ms target")

        self._mutate(f)

    # ---- read surface ----------------------------------------------------
    def health(self) -> dict:
        s = self._load()
        return {
            "service": self.name,
            "status": "unhealthy" if s["broken"] else ("degraded" if s["cpu"] >= 85 else "healthy"),
            "error_rate_pct": 12.5 if s["broken"] else 0.1,
            "cpu_pct": s["cpu"],
            "replicas": s["replicas"],
        }

    def logs(self, lines: int = 100) -> str:
        lines = max(1, min(int(lines), _MAX_LOG_LINES))
        return "\n".join(self._load()["logs"][-lines:])

    # ---- action surface (only called by approved action tools) -----------
    def snapshot(self) -> dict:
        s = self._load()
        return {"broken": s["broken"], "replicas": s["replicas"], "cpu": s["cpu"]}

    def restore(self, snap: dict) -> None:
        def f(s: dict) -> None:
            s["broken"] = bool(snap["broken"])
            s["replicas"] = int(snap["replicas"])
            s["cpu"] = int(snap["cpu"])
            s["logs"].append(f"[info] rolled back to previous state: {snap}")

        self._mutate(f)

    def restart(self) -> None:
        def f(s: dict) -> None:
            s["logs"].append("[info] restart requested - clearing faulty process")
            s["broken"] = False
            s["cpu"] = 30
            s["logs"].append("[info] service recovered, error rate back to normal")

        time.sleep(0.01)
        self._mutate(f)

    def scale(self, replicas: int) -> None:
        def f(s: dict) -> None:
            s["replicas"] = int(replicas)
            s["cpu"] = max(10, s["cpu"] - 30)
            s["logs"].append(f"[info] scaled to {replicas} replicas")

        self._mutate(f)


class Fleet:
    """All simulated services, keyed by name."""

    def __init__(self, db: Store):
        self.db = db
        self._services: dict[str, ServiceSimulator] = {}

    def get(self, name: str) -> ServiceSimulator:
        if name not in self._services:
            self._services[name] = ServiceSimulator(self.db, name)
        return self._services[name]


class PlannerFaults:
    """Lets the demo force planner failures to show retries and dead letters."""

    KEY = "sim:planner_faults"

    def __init__(self, db: Store):
        self.db = db

    def set(self, count: int, kind: str = "transient") -> None:
        self.db.set_kv(self.KEY, {"remaining": max(0, int(count)), "kind": kind})

    def take(self) -> str | None:
        """Consume one injected fault, returning its kind, or None."""
        state = self.db.get_kv(self.KEY)
        if not state or state.get("remaining", 0) <= 0:
            return None
        self.db.set_kv(self.KEY, {**state, "remaining": state["remaining"] - 1})
        return str(state.get("kind", "transient"))


fleet = Fleet(default_store)
sim = fleet.get("web")
planner_faults = PlannerFaults(default_store)
