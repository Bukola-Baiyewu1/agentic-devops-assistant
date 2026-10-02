"""Stage 14 safety controls: webhook signatures, rate limiting, redaction,
production configuration checks, and input validation."""

import json

import pytest

from src.config import ConfigError, Settings, settings, validate_for_startup
from src.redaction import redact, redact_text
from src.security import sign_body
from tests.conftest import make_alert


def signed_post(client, alert, secret="whsec-test"):  # noqa: S107
    body = json.dumps(alert).encode()
    return client.post(
        "/webhook/alert",
        content=body,
        headers={"Content-Type": "application/json", "X-Aegis-Signature": sign_body(body, secret)},
    )


def test_webhook_signature_is_required_when_configured(client):
    settings.webhook_secret = "whsec-test"
    assert client.post("/webhook/alert", json=make_alert("evt-unsigned")).status_code == 401
    assert signed_post(client, make_alert("evt-bad-sig"), secret="wrong").status_code == 401
    assert signed_post(client, make_alert("evt-signed")).status_code == 200


def test_tampered_body_fails_signature(client):
    settings.webhook_secret = "whsec-test"
    body = json.dumps(make_alert("evt-tamper")).encode()
    sig = sign_body(body, "whsec-test")
    tampered = body.replace(b"web", b"api")
    r = client.post(
        "/webhook/alert", content=tampered, headers={"Content-Type": "application/json", "X-Aegis-Signature": sig}
    )
    assert r.status_code == 401


def test_rate_limit_returns_429(client):
    settings.rate_limit_per_minute = 3
    codes = [client.get("/demo/health").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    assert client.get("/health").status_code == 200, "health probes are never rate limited"


@pytest.mark.parametrize("service", ["../etc", "WEB", "web/../../x", "a" * 80, "web;rm"])
def test_invalid_service_names_are_rejected_at_ingress(client, service):
    assert client.post("/webhook/alert", json=make_alert("evt-x", service=service)).status_code == 422


def test_oversized_and_malformed_alerts_are_rejected(client):
    assert client.post("/webhook/alert", json=make_alert(desc="x" * 5000)).status_code == 422
    assert client.post("/webhook/alert", json={"name": "no id"}).status_code == 422
    assert (
        client.post("/webhook/alert", content=b"not json", headers={"Content-Type": "application/json"}).status_code
        == 422
    )
    assert client.post("/webhook/alert", json=make_alert(event_id="bad id with spaces")).status_code == 422


def test_unmanaged_service_alert_escalates(client):
    r = client.post("/webhook/alert", json=make_alert("evt-pay", service="payments")).json()
    assert r["status"] == "escalated" and r["proposed_tool"] is None


def test_security_headers_present(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_redaction_patterns():
    text = (
        "key sk-ant-api03-abcdefghijklmnop aws AKIAABCDEFGHIJKLMNOP "
        "Authorization: Bearer abc.def.ghi password=hunter2 "
        "postgresql://aegis:s3cret@db:5432/aegis mail me@example.com"
    )
    out = redact_text(text)
    for secret in (
        "sk-ant-api03-abcdefghijklmnop",
        "AKIAABCDEFGHIJKLMNOP",
        "abc.def.ghi",
        "hunter2",
        "s3cret",
        "me@example.com",
    ):
        assert secret not in out.text
    assert out.total >= 6
    assert "postgresql://aegis:[REDACTED]@db" in out.text


def test_redaction_keeps_ordinary_text_and_token_counts():
    assert redact_text("500 errors after deploy on web").text == "500 errors after deploy on web"
    assert redact({"input_tokens": 10, "approval_token": "abc", "nested": {"api_key": "k"}}) == {
        "input_tokens": 10,
        "approval_token": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
    }


def test_logs_never_contain_approval_secrets(client, caplog):
    client.post("/demo/break", json={})
    action_id = client.post("/webhook/alert", json=make_alert()).json()["action_id"]
    token = client.get(f"/actions/{action_id}/challenge").json()["token"]
    client.post(f"/actions/{action_id}/approve", json={"token": "wrong-token"})
    r = client.post(f"/actions/{action_id}/approve", json={"token": token, "execute": False})
    cap = r.json()["capability"]
    out = caplog.text
    assert '"event": "action_approved"' in out
    assert token not in out and cap not in out


def test_production_refuses_insecure_defaults():
    with pytest.raises(ConfigError) as exc:
        validate_for_startup(Settings(env="production"))
    msg = str(exc.value)
    for needle in ("AEGIS_SECRET_KEY", "AEGIS_WEBHOOK_SECRET", "AEGIS_USERS", "PostgreSQL"):
        assert needle in msg


def test_production_accepts_secure_config():
    validate_for_startup(
        Settings(
            env="production",
            secret_key="x" * 40,
            webhook_secret="whsec",
            users="ops:sha256:" + "0" * 64,
            database_url="postgresql+psycopg://aegis:pw@db/aegis",
        )
    )


def test_production_has_no_default_demo_user(client):
    settings.env = "production"
    settings.users = ""
    r = client.get("/actions", auth=("demo", "demo"))
    assert r.status_code == 401


def test_dotenv_loader_does_not_override_real_environment(tmp_path, monkeypatch):
    from src.config import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nAEGIS_T1='from file'\nAEGIS_T2=file\nexport AEGIS_T3=x\nnot a pair\n")
    monkeypatch.setenv("AEGIS_T2", "real")
    monkeypatch.delenv("AEGIS_T1", raising=False)
    monkeypatch.delenv("AEGIS_T3", raising=False)
    load_dotenv(str(env_file))
    import os

    assert os.environ["AEGIS_T1"] == "from file"
    assert os.environ["AEGIS_T2"] == "real"
    assert os.environ["AEGIS_T3"] == "x"
    for k in ("AEGIS_T1", "AEGIS_T3"):
        monkeypatch.delenv(k)
