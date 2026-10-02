"""Model and retrieval tracing with Langfuse (optional).

When LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set, each alert produces
one Langfuse trace with child observations for observation, retrieval, every
model call (with token usage), validation, approval, execution, and rollback.
Without credentials every call is a no-op, so the project runs offline.

All inputs and outputs pass through the redactor before they are sent.
Approval tokens and capabilities are never passed to the tracer at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .config import settings
from .observability import log
from .redaction import redact


class _NoopObservation:
    def update(self, **_: Any) -> None:
        return None


class Tracer:
    def __init__(self, client: Any | None = None):
        self._client = client

    @classmethod
    def from_settings(cls) -> Tracer:
        if not settings.langfuse_enabled():
            return cls(None)
        try:
            from langfuse import Langfuse

            kwargs: dict[str, Any] = {
                "public_key": settings.langfuse_public_key,
                "secret_key": settings.langfuse_secret_key,
                "environment": settings.env,
            }
            if settings.langfuse_base_url:
                kwargs["base_url"] = settings.langfuse_base_url
            return cls(Langfuse(**kwargs))
        except Exception as exc:  # telemetry must never break the agent
            log("langfuse_init_failed", error=type(exc).__name__)
            return cls(None)

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @contextmanager
    def observe(
        self,
        name: str,
        *,
        as_type: str = "span",
        input: Any = None,
        metadata: Any = None,
        model: str | None = None,
    ) -> Iterator[Any]:
        if self._client is None:
            yield _NoopObservation()
            return
        kwargs: dict[str, Any] = {"name": name, "as_type": as_type}
        if input is not None:
            kwargs["input"] = redact(input)
        if metadata is not None:
            kwargs["metadata"] = redact(metadata)
        if model:
            kwargs["model"] = model
        with self._client.start_as_current_observation(**kwargs) as obs:
            yield _SafeObservation(obs)

    def flush(self) -> None:
        if self._client is not None:
            try:
                self._client.flush()
            except Exception as exc:
                log("langfuse_flush_failed", error=type(exc).__name__)


class _SafeObservation:
    """Wraps a Langfuse observation so every update is redacted."""

    def __init__(self, obs: Any):
        self._obs = obs

    def update(self, **kwargs: Any) -> None:
        for key in ("input", "output", "metadata"):
            if key in kwargs:
                kwargs[key] = redact(kwargs[key])
        self._obs.update(**kwargs)


tracer = Tracer.from_settings()


def set_tracer(t: Tracer) -> None:
    """Replace the global tracer (used by tests)."""
    global tracer
    tracer = t


def get_tracer() -> Tracer:
    return tracer
