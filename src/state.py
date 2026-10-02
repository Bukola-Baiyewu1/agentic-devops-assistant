"""A tiny thread-safe, JSON-backed store.

This is deliberately simple so you can open the file and see exactly what the
system is doing. In production you would swap this for Postgres or Redis — the
method names would stay the same.
"""
import json
import os
import threading
import uuid
from typing import Optional

from . import config


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data = {"events": {}, "actions": {}, "traces": []}
        self._load()

    # ---- persistence -------------------------------------------------
    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass  # start fresh if the file is missing or corrupt

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)  # atomic write

    def clear(self):
        with self._lock:
            self._data = {"events": {}, "actions": {}, "traces": []}
            self._save()

    # ---- idempotency -------------------------------------------------
    def event_seen(self, event_id: str) -> Optional[str]:
        """Return the action_id already created for this event, if any."""
        return self._data["events"].get(event_id)

    def remember_event(self, event_id: str, action_id: str):
        with self._lock:
            self._data["events"][event_id] = action_id
            self._save()

    # ---- actions -----------------------------------------------------
    def create_action(self, record: dict) -> str:
        action_id = record.get("id") or str(uuid.uuid4())[:8]
        record["id"] = action_id
        with self._lock:
            self._data["actions"][action_id] = record
            self._save()
        return action_id

    def get_action(self, action_id: str) -> Optional[dict]:
        return self._data["actions"].get(action_id)

    def update_action(self, action_id: str, **changes):
        with self._lock:
            self._data["actions"][action_id].update(changes)
            self._save()

    def list_actions(self) -> list:
        return list(self._data["actions"].values())

    # ---- traces ------------------------------------------------------
    def add_trace(self, record: dict):
        with self._lock:
            self._data["traces"].append(record)
            self._save()

    def list_traces(self) -> list:
        return list(self._data["traces"])


store = Store(config.STATE_PATH)
