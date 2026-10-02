"""Stage 11: correlation IDs, Prometheus metrics, and Langfuse traces."""

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src import tracing
from src.config import settings
from tests.conftest import challenge, make_alert


def metric_value(text, name, labels=""):
    for line in text.splitlines():
        if line.startswith(name + labels + " ") or (not labels and line.startswith(name + " ")):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_correlation_id_is_echoed_or_generated(client):
    r = client.get("/health", headers={"X-Correlation-ID": "abc12345-trace"})
    assert r.headers["x-correlation-id"] == "abc12345-trace"
    generated = client.get("/health").headers["x-correlation-id"]
    assert len(generated) == 32
    bad = client.get("/health", headers={"X-Correlation-ID": "<script>"}).headers["x-correlation-id"]
    assert bad != "<script>"


def test_trace_record_and_log_share_the_correlation_id(client, caplog):
    client.post("/webhook/alert", json=make_alert("evt-cid"), headers={"X-Correlation-ID": "cid-0000-1111"})
    trace = client.get("/traces").json()[0]
    assert trace["correlation_id"] == "cid-0000-1111"
    assert trace["event_id"] == "evt-cid"
    for key in ("latency_ms", "cost_usd", "input_tokens", "output_tokens", "citation", "decision", "mode"):
        assert key in trace
    out = caplog.text
    assert '"correlation_id": "cid-0000-1111"' in out


def test_metrics_cover_approval_denial_duplicate_retry_and_dead_letter(client):
    before = client.get("/metrics").text
    client.post("/demo/break", json={})
    a1 = client.post("/webhook/alert", json=make_alert("m-1")).json()["action_id"]
    client.post(f"/actions/{a1}/approve", json={"token": challenge(client, a1)})
    client.post("/demo/break", json={})
    a2 = client.post("/webhook/alert", json=make_alert("m-2")).json()["action_id"]
    client.post(f"/actions/{a2}/deny", json={"token": challenge(client, a2)})
    client.post("/webhook/alert", json=make_alert("m-2"))  # duplicate
    settings.max_event_attempts = 2
    client.post("/demo/planner-faults", json={"count": 5, "kind": "transient"})
    client.post("/webhook/alert", json=make_alert("m-3"))  # retry scheduled
    import time

    from src import events

    events.run_due(now=time.time() + 10_000)  # second failure -> dead letter
    after = client.get("/metrics").text

    def delta(name, labels=""):
        return metric_value(after, name, labels) - metric_value(before, name, labels)

    assert delta("aegis_approvals_total", '{decision="approved"}') == 1
    assert delta("aegis_approvals_total", '{decision="denied"}') == 1
    assert delta("aegis_duplicate_alerts_total") == 1
    assert delta("aegis_event_retries_total") == 1
    assert delta("aegis_dead_letters_total") == 1
    assert delta("aegis_tool_executions_total", '{result="ok",tool="restart_service"}') == 1
    assert "aegis_plan_latency_seconds_bucket" in after
    assert "aegis_llm_cost_usd_total" in after


def test_metrics_token_when_configured(client, anon):
    settings.metrics_token = "scrape-secret"
    assert anon.get("/metrics").status_code == 401
    assert anon.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 200


def test_langfuse_receives_redacted_spans(client):
    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    lf = Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        base_url="http://localhost:9",
        span_exporter=exporter,
        flush_at=1,
    )
    tracing.set_tracer(tracing.Tracer(lf))
    client.post("/demo/break", json={})
    alert = make_alert("evt-lf", desc="500 errors, password=hunter2")
    action_id = client.post("/webhook/alert", json=alert).json()["action_id"]
    token = challenge(client, action_id)
    client.post(f"/actions/{action_id}/approve", json={"token": token})
    lf.flush()
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"triage", "observe-and-retrieve", "verify-proposal", "human-approval", "execute-tool"} <= names
    exported = " ".join(str(dict(s.attributes)) for s in spans)
    assert "hunter2" not in exported
    assert token not in exported


def test_tracer_is_noop_without_credentials():
    t = tracing.Tracer.from_settings()
    assert not t.enabled
    with t.observe("x", input={"a": 1}) as obs:
        obs.update(output={"b": 2})


def test_langfuse_records_each_model_call_with_token_usage():
    from langfuse import Langfuse

    from src import agent
    from tests.conftest import make_alert
    from tests.test_planner_claude import planner, propose, use

    exporter = InMemorySpanExporter()
    lf = Langfuse(
        public_key="pk-lf-test-planner",
        secret_key="sk-lf-test",
        base_url="http://localhost:9",
        span_exporter=exporter,
        flush_at=1,
    )
    tracing.set_tracer(tracing.Tracer(lf))
    agent.plan(make_alert(), planner=planner(use("get_service_health", {"service": "web"}), propose()))
    lf.flush()
    gens = [s for s in exporter.get_finished_spans() if s.name.startswith("llm-call-")]
    assert [s.name for s in gens] == ["llm-call-1", "llm-call-2"]
    attrs = " ".join(str(dict(s.attributes)) for s in gens)
    assert "1000" in attrs and "200" in attrs  # input/output token usage reported
