"""Background worker: processes queued and retrying events, reclaims events
whose worker died, and expires approval windows.

Run it with:  python -m src.worker
It also serves its own Prometheus metrics on WORKER_METRICS_PORT (default 9100).
"""

from __future__ import annotations

import os
import signal
import time
from types import FrameType

from prometheus_client import start_http_server

from . import approval, events
from .config import settings, validate_for_startup
from .observability import log, registry
from .tracing import get_tracer

_running = True


def _stop(signum: int, frame: FrameType | None) -> None:
    global _running
    _running = False
    log("worker_stopping", signal=signum)


def run_once() -> dict:
    processed = events.run_due()
    expired = approval.expire_stale()
    return {"processed": processed, "expired": expired}


def main() -> None:
    validate_for_startup()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    port = int(os.getenv("WORKER_METRICS_PORT", "9100"))
    if port:
        start_http_server(port, registry=registry)
    log("worker_started", poll_seconds=settings.worker_poll_seconds, metrics_port=port)
    while _running:
        try:
            run_once()
        except Exception as exc:  # keep the worker alive; the error is logged
            log("worker_loop_error", error=f"{type(exc).__name__}: {exc}")
        time.sleep(settings.worker_poll_seconds)
    get_tracer().flush()
    log("worker_stopped")


if __name__ == "__main__":
    main()
