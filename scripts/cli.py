"""Command-line client for a running Aegis server (works on Windows, macOS, Linux).

It talks to the server over HTTP, so it uses the same approval rules as the
web page. Configure it with environment variables:

  AEGIS_URL              default http://localhost:8000
  AEGIS_USER             default demo
  AEGIS_PASSWORD         default demo
  AEGIS_WEBHOOK_SECRET   only if the server verifies webhook signatures

Usage:
  python -m scripts.cli demo                      break the demo service and send an alert
  python -m scripts.cli alert <event_id> "<name>" ["<description>"] [service]
  python -m scripts.cli list                      list actions
  python -m scripts.cli show <action_id>          action details + audit trail
  python -m scripts.cli approve <action_id>       approve and execute
  python -m scripts.cli deny <action_id> [reason]
  python -m scripts.cli rollback <action_id>      request a rollback (step 1)
  python -m scripts.cli approve-rollback <action_id>   approve the rollback (step 2)
  python -m scripts.cli faults <count> [kind]     make the next planning attempts fail (transient|permanent)
  python -m scripts.cli events [status]           list events (e.g. dead_lettered)
  python -m scripts.cli replay <event_id>         replay a dead-lettered event
  python -m scripts.cli health                    demo service health
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import uuid

import httpx

BASE = os.getenv("AEGIS_URL", "http://localhost:8000").rstrip("/")
AUTH = (os.getenv("AEGIS_USER", "demo"), os.getenv("AEGIS_PASSWORD", "demo"))
JSON = {"Content-Type": "application/json"}


def _client() -> httpx.Client:
    return httpx.Client(base_url=BASE, auth=AUTH, timeout=60)


def _print(r: httpx.Response) -> None:
    try:
        body = r.json()
    except ValueError:
        body = r.text
    print(f"HTTP {r.status_code}")
    print(json.dumps(body, indent=2) if not isinstance(body, str) else body)


def send_alert(
    c: httpx.Client, event_id: str, name: str, description: str = "", service: str = "web"
) -> httpx.Response:
    body = json.dumps({"event_id": event_id, "name": name, "description": description, "service": service}).encode()
    headers = dict(JSON)
    secret = os.getenv("AEGIS_WEBHOOK_SECRET", "")
    if secret:
        headers["X-Aegis-Signature"] = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return c.post("/webhook/alert", content=body, headers=headers)


def _challenge(c: httpx.Client, action_id: str) -> str:
    r = c.get(f"/actions/{action_id}/challenge")
    if r.status_code != 200:
        _print(r)
        sys.exit(1)
    return r.json()["token"]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 0
    cmd, args = argv[1], argv[2:]
    with _client() as c:
        if cmd == "demo":
            print("1) Breaking the demo service...")
            _print(c.post("/demo/break", json={}))
            print("\n2) Sending an alert...")
            r = send_alert(
                c, f"demo-{uuid.uuid4().hex[:8]}", "High 5xx error rate", "500 errors began right after a deploy"
            )
            _print(r)
            action_id = r.json().get("action_id")
            if action_id:
                print(f"\n-> Approve in the browser: {BASE}/approve/{action_id}")
                print(f"-> Or from here:            python -m scripts.cli approve {action_id}")
        elif cmd == "alert" and len(args) >= 2:
            _print(
                send_alert(c, args[0], args[1], args[2] if len(args) > 2 else "", args[3] if len(args) > 3 else "web")
            )
        elif cmd == "list":
            for a in c.get("/actions").raise_for_status().json():
                cite = (a.get("citation") or {}).get("chunk_id", "-")
                print(f"{a['id']}  [{a['status']:17}]  {a.get('tool_name') or '-':16}  <- {cite}")
        elif cmd == "show" and args:
            _print(c.get(f"/actions/{args[0]}"))
            _print(c.get(f"/actions/{args[0]}/audit"))
        elif cmd == "approve" and args:
            _print(c.post(f"/actions/{args[0]}/approve", json={"token": _challenge(c, args[0])}))
        elif cmd == "deny" and args:
            reason = args[1] if len(args) > 1 else ""
            _print(c.post(f"/actions/{args[0]}/deny", json={"token": _challenge(c, args[0]), "reason": reason}))
        elif cmd == "rollback" and args:
            _print(c.post(f"/actions/{args[0]}/rollback", json={}))
        elif cmd == "approve-rollback" and args:
            _print(c.post(f"/actions/{args[0]}/rollback/approve", json={"token": _challenge(c, args[0])}))
        elif cmd == "events":
            params = {"status": args[0]} if args else {}
            for e in c.get("/events", params=params).raise_for_status().json():
                print(
                    f"{e['event_id']:30} [{e['status']:13}] attempts={e['attempts']} action={e['action_id']} {e['last_error'] or ''}"
                )
        elif cmd == "faults" and args:
            kind = args[1] if len(args) > 1 else "transient"
            _print(c.post("/demo/planner-faults", json={"count": int(args[0]), "kind": kind}))
        elif cmd == "replay" and args:
            _print(c.post(f"/events/{args[0]}/replay", json={}))
        elif cmd == "health":
            _print(c.get("/demo/health"))
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
