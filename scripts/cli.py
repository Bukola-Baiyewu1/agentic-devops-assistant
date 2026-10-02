"""Command-line approval tool — a thin client for the running Aegis server.

It talks to the server over HTTP so approvals affect the live service (the same
source of truth the web UI uses). Point it at another host with AEGIS_URL.

Usage:
  python -m scripts.cli list
  python -m scripts.cli approve <action_id>
  python -m scripts.cli deny <action_id>
  python -m scripts.cli rollback <action_id>
"""
import json
import os
import sys

import httpx

BASE = os.getenv("AEGIS_URL", "http://localhost:8000")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]

    if cmd == "list":
        for a in httpx.get(f"{BASE}/actions").json():
            p = a["proposal"]
            print(f"{a['id']}  [{a['status']:16}]  {p['tool_name']}  <- {p['citation_source']}")
        return

    action_id = sys.argv[2]
    if cmd == "approve":
        token = httpx.get(f"{BASE}/actions/{action_id}").json().get("approval_token") or ""
        r = httpx.post(f"{BASE}/actions/{action_id}/approve", json={"token": token})
    elif cmd == "deny":
        r = httpx.post(f"{BASE}/actions/{action_id}/deny")
    elif cmd == "rollback":
        r = httpx.post(f"{BASE}/actions/{action_id}/rollback")
    else:
        print(__doc__)
        return
    print(json.dumps(r.json(), indent=2))


if __name__ == "__main__":
    main()
