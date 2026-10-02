"""The web service: webhook ingress, approval UI, demo controls, traces.

Run it with:  uvicorn src.app:app --reload
"""
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import agent, approval
from .demo import sim
from .state import store
from .observability import log

app = FastAPI(title="Aegis — Agentic DevOps Assistant")


class Alert(BaseModel):
    event_id: str
    name: str
    description: str = ""
    service: str = "web"


@app.get("/health")
def health():
    return {"ok": True}


# ---- 1) event ingress (idempotent) ----------------------------------
@app.post("/webhook/alert")
def webhook_alert(alert: Alert):
    # Idempotency: the same event_id never triggers a second plan.
    existing = store.event_seen(alert.event_id)
    if existing:
        log("duplicate_event", event_id=alert.event_id, action_id=existing)
        return {"action_id": existing, "duplicate": True, **_summary(existing)}

    result = agent.plan(alert.model_dump())
    action = approval.create_pending_action(alert.model_dump(), result)
    store.remember_event(alert.event_id, action["id"])
    return {"action_id": action["id"], "duplicate": False, **_summary(action["id"])}


def _summary(action_id: str) -> dict:
    a = store.get_action(action_id)
    p = a["proposal"]
    return {
        "status": a["status"],
        "plan": p["reasoning"],
        "citation": p["citation_source"],
        "proposed_tool": p["tool_name"],
        "approve_url": f"/approve/{action_id}" if a["status"] == "pending_approval" else None,
    }


# ---- 2) actions + approval ------------------------------------------
@app.get("/actions")
def list_actions():
    return store.list_actions()


@app.get("/actions/{action_id}")
def get_action(action_id: str):
    a = store.get_action(action_id)
    if not a:
        raise HTTPException(404, "action not found")
    return a


class TokenBody(BaseModel):
    token: str


@app.post("/actions/{action_id}/approve")
def approve_action(action_id: str, body: TokenBody):
    a = store.get_action(action_id)
    if not a:
        raise HTTPException(404, "action not found")

    if a["status"] != "pending_approval":
        raise HTTPException(409, "action is not pending approval")

    expected_token = a.get("approval_token")
    if not expected_token or body.token != expected_token:
        raise HTTPException(403, "invalid approval token")

    try:
        return approval.approve(action_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/actions/{action_id}/deny")
def deny_action(action_id: str):
    try:
        return approval.deny(action_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/actions/{action_id}/rollback")
def rollback_action(action_id: str):
    try:
        return approval.rollback(action_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---- 3) a tiny human approval page ----------------------------------
@app.get("/approve/{action_id}", response_class=HTMLResponse)
def approve_page(action_id: str):
    a = store.get_action(action_id)
    if not a:
        raise HTTPException(404, "action not found")
    p = a["proposal"]
    token = a["approval_token"] or ""
    return f"""
    <html><body style="font-family:system-ui;max-width:640px;margin:40px auto">
      <h2>Approval required — action {action_id}</h2>
      <p><b>Alert:</b> {a['alert']['name']}</p>
      <p><b>Proposed:</b> {p['tool_name']} {p['tool_args']}</p>
      <p><b>Grounded in:</b> {p['citation_source']}</p>
      <blockquote>{p['reasoning']}</blockquote>
      <p><b>Status:</b> {a['status']}</p>
      <button onclick="act('approve')" style="padding:10px 18px">Approve</button>
      <button onclick="act('deny')" style="padding:10px 18px">Deny</button>
      <pre id="out"></pre>
      <script>
        async function act(kind) {{
          const body = kind === 'approve' ? JSON.stringify({{token: '{token}'}}) : '{{}}';
          const r = await fetch(`/actions/{action_id}/${{kind}}`, {{
            method:'POST', headers:{{'Content-Type':'application/json'}}, body}});
          document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
        }}
      </script>
    </body></html>
    """


# ---- 4) observability + demo controls -------------------------------
@app.get("/traces")
def traces():
    return store.list_traces()


@app.post("/demo/break")
def demo_break():
    sim.inject_error()
    return sim.health()


@app.post("/demo/fix")
def demo_fix():
    sim.clear_error()
    return sim.health()


@app.get("/demo/health")
def demo_health():
    return sim.health()


@app.get("/demo/logs", response_class=JSONResponse)
def demo_logs():
    return {"logs": sim.logs(50)}
