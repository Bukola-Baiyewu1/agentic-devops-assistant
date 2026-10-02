"""Structured logs with correlation IDs, Prometheus metrics, timing, and cost.

* Every log line is one JSON object that carries the current correlation ID,
  so a single alert can be followed across the API, worker, and MCP server.
* Every log line passes through the redactor: secrets and approval tokens are
  never written to logs.
* Metrics are exposed at /metrics in Prometheus format.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Histogram

from .config import settings
from .redaction import redact

# ---------------------------------------------------------------- correlation
_correlation_id: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def set_correlation_id(value: str) -> contextvars.Token[str]:
    return _correlation_id.set(value)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    _correlation_id.reset(token)


def get_correlation_id() -> str:
    return _correlation_id.get()


# -------------------------------------------------------------------- logging
logger = logging.getLogger("aegis")
if not logger.handlers:
    # stderr, so logs never mix with the MCP stdio protocol on stdout
    _h = logging.StreamHandler(sys.stderr)
    _h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log(event: str, **fields: Any) -> None:
    """Emit one structured, redacted JSON log line."""
    record = {"ts": round(time.time(), 3), "event": event}
    cid = get_correlation_id()
    if cid:
        record["correlation_id"] = cid
    record.update(redact(fields))
    logger.info(json.dumps(record, default=str))


# -------------------------------------------------------------------- metrics
registry = CollectorRegistry()

ALERTS = Counter("aegis_alerts_total", "Alerts received, by outcome", ["status"], registry=registry)
DUPLICATES = Counter("aegis_duplicate_alerts_total", "Duplicate alerts ignored", registry=registry)
PLANS = Counter("aegis_plans_total", "Planner decisions", ["decision", "mode"], registry=registry)
UNSAFE_REJECTED = Counter(
    "aegis_unsafe_proposals_rejected_total", "Proposals rejected by validation", ["reason"], registry=registry
)
APPROVALS = Counter("aegis_approvals_total", "Human decisions", ["decision"], registry=registry)
TOOL_EXECUTIONS = Counter(
    "aegis_tool_executions_total", "Action tool executions", ["tool", "result"], registry=registry
)
ROLLBACKS = Counter("aegis_rollbacks_total", "Rollbacks", ["result"], registry=registry)
RETRIES = Counter("aegis_event_retries_total", "Event processing retries scheduled", registry=registry)
DEAD_LETTERS = Counter("aegis_dead_letters_total", "Events moved to dead letter", registry=registry)
PLAN_LATENCY = Histogram(
    "aegis_plan_latency_seconds",
    "Planning latency",
    registry=registry,
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
)
TOOL_LATENCY = Histogram(
    "aegis_tool_latency_seconds",
    "Action tool latency",
    ["tool"],
    registry=registry,
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
LLM_TOKENS = Counter("aegis_llm_tokens_total", "Model tokens", ["direction"], registry=registry)
LLM_COST = Counter("aegis_llm_cost_usd_total", "Estimated model cost in USD", registry=registry)


# ------------------------------------------------------------------ utilities
def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimated cost from configured per-million-token prices (not billing truth)."""
    cost = (input_tokens / 1_000_000) * settings.llm_input_price_per_mtok + (
        output_tokens / 1_000_000
    ) * settings.llm_output_price_per_mtok
    return round(cost, 6)


class Timer:
    ms: float = 0.0
    seconds: float = 0.0

    def __enter__(self) -> Timer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self._start
        self.ms = round(self.seconds * 1000, 1)
