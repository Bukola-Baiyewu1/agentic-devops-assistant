"""End-to-end smoke test against a running Aegis (local Compose or a deployment).

  python scripts/smoke_test.py http://localhost:8000

Uses AEGIS_USER / AEGIS_PASSWORD (default demo/demo), AEGIS_WEBHOOK_SECRET if
the server verifies webhook signatures, and AEGIS_METRICS_TOKEN if /metrics is protected. Exits non-zero on the first failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import httpx


def check(cond: bool, msg: str) -> None:
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


def main(base: str) -> None:
    auth = (os.getenv("AEGIS_USER", "demo"), os.getenv("AEGIS_PASSWORD", "demo"))
    c = httpx.Client(base_url=base.rstrip("/"), auth=auth, timeout=30)
    for _ in range(60):
        try:
            if c.get("/ready").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(2)
    check(c.get("/ready").status_code == 200, "service is ready")
    check(httpx.get(base.rstrip("/") + "/actions").status_code == 401, "actions require login")

    c.post("/demo/fix", json={})
    c.post("/demo/break", json={})
    check(c.get("/demo/health").json()["status"] == "unhealthy", "demo service broken")

    body = json.dumps(
        {
            "event_id": f"smoke-{uuid.uuid4().hex[:8]}",
            "name": "High 5xx error rate",
            "description": "500 errors after deploy",
            "service": "web",
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    secret = os.getenv("AEGIS_WEBHOOK_SECRET", "")
    if secret:
        headers["X-Aegis-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = c.post("/webhook/alert", content=body, headers=headers)
    if resp.status_code == 401:
        print(
            "FAIL alert produced a pending, cited proposal: 401, the webhook signature was rejected. "
            "Set AEGIS_WEBHOOK_SECRET to the secret the deploy printed"
            + (" (the one you set does not match)." if secret else " (it is not set in this shell).")
        )
        sys.exit(1)
    r = resp.json()
    if r.get("status") != "pending_approval":
        print(f"     HTTP {resp.status_code}: {json.dumps(r)[:300]}")
    check(r.get("status") == "pending_approval", "alert produced a pending, cited proposal")
    check(bool(r.get("citation")), "proposal cites a runbook")
    action_id = r["action_id"]
    check(c.get("/demo/health").json()["status"] == "unhealthy", "nothing ran before approval")

    check(c.post(f"/actions/{action_id}/approve", json={"token": "wrong"}).status_code == 403, "wrong token refused")
    token = c.get(f"/actions/{action_id}/challenge").json()["token"]
    done = c.post(f"/actions/{action_id}/approve", json={"token": token}).json()
    check(done.get("status") == "executed", "approved action executed")
    check(c.get("/demo/health").json()["status"] == "healthy", "service recovered")
    check(token not in json.dumps(c.get(f"/actions/{action_id}").json()), "token not exposed in action JSON")

    c.post(f"/actions/{action_id}/rollback", json={})
    rb_token = c.get(f"/actions/{action_id}/challenge").json()["token"]
    rb = c.post(f"/actions/{action_id}/rollback/approve", json={"token": rb_token}).json()
    check(rb.get("status") == "rolled_back", "rollback approved separately and applied")
    metrics_token = os.getenv("AEGIS_METRICS_TOKEN", "")
    headers = {"Authorization": f"Bearer {metrics_token}"} if metrics_token else {}
    m = httpx.get(base.rstrip("/") + "/metrics", headers=headers)
    if m.status_code == 401:
        print(
            "FAIL metrics exposed: 401, the server wants a metrics token. Set AEGIS_METRICS_TOKEN to the "
            "token the deploy printed"
            + (" (the one you set does not match)." if metrics_token else " (it is not set in this shell).")
        )
        sys.exit(1)
    check(m.status_code == 200 and "aegis_approvals_total" in m.text, f"metrics exposed (HTTP {m.status_code})")
    c.post("/demo/fix", json={})
    print("smoke test passed")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000")
