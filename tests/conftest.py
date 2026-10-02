"""Shared test setup.

Environment variables are set BEFORE the application is imported so that every
test runs against a throwaway SQLite database, the deterministic mock planner,
and known approver accounts. No test ever calls a paid model API.
"""

import hashlib
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="aegis-test-")
os.environ.update(
    {
        # Set AEGIS_TEST_DATABASE_URL to run the suite against PostgreSQL (CI does).
        "DATABASE_URL": os.getenv("AEGIS_TEST_DATABASE_URL", f"sqlite:///{_TMP}/test.db"),
        "AEGIS_ENV": "development",
        "AEGIS_LLM_PROVIDER": "mock",
        "ANTHROPIC_API_KEY": "",
        "AEGIS_SECRET_KEY": "test-secret-key-that-is-long-enough-123456",
        # alice: plain text (development style); bob: sha256 hash (production style)
        "AEGIS_USERS": "alice:alice-pass,bob:sha256:" + hashlib.sha256(b"bob-pass").hexdigest(),
        "AEGIS_WEBHOOK_SECRET": "",
        "AEGIS_METRICS_TOKEN": "",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "AEGIS_RATE_LIMIT_PER_MINUTE": "100000",
        "AEGIS_PROCESS_INLINE": "true",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src import rag as rag_module  # noqa: E402
from src import tracing  # noqa: E402
from src.app import app  # noqa: E402
from src.config import settings  # noqa: E402
from src.demo import sim  # noqa: E402
from src.security import limiter  # noqa: E402
from src.state import store  # noqa: E402

ALICE = ("alice", "alice-pass")
BOB = ("bob", "bob-pass")


@pytest.fixture(autouse=True)
def clean_state():
    """Fresh database, service, settings, retriever, and tracer for every test."""
    saved = settings.model_dump()
    default_retriever = rag_module.get_retriever()
    store.reset()
    sim.reset()
    limiter.reset()
    tracing.set_tracer(tracing.Tracer(None))
    yield
    for key, value in saved.items():
        setattr(settings, key, value)
    rag_module.set_retriever(default_retriever)
    tracing.set_tracer(tracing.Tracer(None))
    store.reset()
    sim.reset()


@pytest.fixture
def client():
    """An HTTP client logged in as approver alice."""
    with TestClient(app) as c:
        c.auth = ALICE
        yield c


@pytest.fixture
def anon():
    """An HTTP client with no credentials."""
    with TestClient(app) as c:
        yield c


def make_alert(event_id="evt-1", name="High 5xx error rate", desc="500 errors after deploy", service="web"):
    return {"event_id": event_id, "name": name, "description": desc, "service": service}


def challenge(client, action_id):
    r = client.get(f"/actions/{action_id}/challenge")
    assert r.status_code == 200, r.text
    return r.json()["token"]


def broken_alert(client, event_id="evt-1", **kw):
    """Break the demo service, send an alert, and return the action id."""
    client.post("/demo/break", json={})
    r = client.post("/webhook/alert", json=make_alert(event_id, **kw))
    assert r.status_code == 200, r.text
    return r.json()["action_id"]
