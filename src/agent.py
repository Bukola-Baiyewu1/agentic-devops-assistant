"""The agent core: observe -> retrieve -> plan -> self-check.

The planner NEVER executes an action. It only *proposes* one. Execution happens
later, and only after a human approves (see approval.py). The planner's output is
a strict JSON schema (structured output), and any plan that fails to cite a
retrieved runbook is rejected and escalated to a human.
"""
import json
from typing import Optional

from pydantic import BaseModel, ValidationError

from . import config, tools
from .observability import Timer, estimate_cost, log


class Proposal(BaseModel):
    reasoning: str
    citation_source: Optional[str] = None   # a runbook filename it is following
    tool_name: Optional[str] = None         # restart_service | scale_service | None
    tool_args: dict = {}
    needs_human: bool = False
    confidence: float = 0.5


SYSTEM_PROMPT = """You are a cautious DevOps triage assistant. You NEVER take
action yourself; you only propose one action for a human to approve.

Rules:
- Base your plan on the provided runbook excerpts. Cite the runbook filename you
  are following in `citation_source`. If no runbook supports an action, set
  needs_human=true and propose no tool.
- Allowed tools: restart_service(service), scale_service(service, replicas).
- Respond with ONLY a JSON object matching this schema:
  {"reasoning": str, "citation_source": str|null, "tool_name": str|null,
   "tool_args": object, "needs_human": bool, "confidence": number}
"""


def _build_context(alert: dict) -> dict:
    service = alert.get("service", "web")
    query = f"{alert.get('name','')} {alert.get('description','')}".strip()
    return {
        "service": service,
        "query": query,
        "health": tools.get_service_health(service),
        "logs": tools.get_recent_logs(service, 20),
        "runbooks": tools.search_runbooks(query or alert.get("name", "")),
    }


# ---- mock planner (used when no API key is set) ----------------------
def _mock_plan(alert: dict, ctx: dict) -> Proposal:
    text = f"{alert.get('name','')} {alert.get('description','')}".lower()
    top = ctx["runbooks"][0] if ctx["runbooks"] else None
    source = top["source"] if top else None

    if any(w in text for w in ["5xx", "error", "500", "exception"]):
        return Proposal(
            reasoning=f"Errors detected on '{ctx['service']}'. Per {source} the first "
                      f"remediation is to restart the service to clear the faulty process.",
            citation_source=source, tool_name="restart_service",
            tool_args={"service": ctx["service"]}, confidence=0.8,
        )
    if any(w in text for w in ["cpu", "memory", "saturat", "latency", "slow"]):
        return Proposal(
            reasoning=f"Resource saturation on '{ctx['service']}'. Per {source} scale out "
                      f"by one replica.",
            citation_source=source, tool_name="scale_service",
            tool_args={"service": ctx["service"], "replicas": ctx["health"]["replicas"] + 1},
            confidence=0.7,
        )
    return Proposal(
        reasoning="No matching runbook step for this alert. Escalating to a human.",
        citation_source=source, needs_human=True, confidence=0.3,
    )


# ---- real planner (Claude via the Anthropic SDK) ---------------------
def _claude_plan(alert: dict, ctx: dict):
    import anthropic  # imported lazily so the mock path needs no dependency
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    runbook_text = "\n\n".join(f"[{r['source']}]\n{r['text']}" for r in ctx["runbooks"])
    user = (
        f"ALERT: {json.dumps(alert)}\n\n"
        f"HEALTH: {json.dumps(ctx['health'])}\n\n"
        f"RECENT LOGS:\n{ctx['logs']}\n\n"
        f"RUNBOOK EXCERPTS:\n{runbook_text}\n\n"
        f"Propose one action as JSON."
    )
    resp = client.messages.create(
        model=config.AGENT_MODEL, max_tokens=600,
        system=SYSTEM_PROMPT, messages=[{"role": "user", "content": user}],
    )
    raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    proposal = Proposal(**json.loads(raw))
    usage = resp.usage
    return proposal, usage.input_tokens, usage.output_tokens


def plan(alert: dict) -> dict:
    """Run the full observe->retrieve->plan->self-check pipeline for one alert."""
    ctx = _build_context(alert)
    input_tokens = output_tokens = 0

    with Timer() as t:
        if config.use_real_llm():
            try:
                proposal, input_tokens, output_tokens = _claude_plan(alert, ctx)
            except (json.JSONDecodeError, ValidationError, Exception) as e:  # graceful degradation
                log("planner_error", error=str(e))
                proposal = Proposal(reasoning=f"Planner failed ({e}); escalating.",
                                    needs_human=True, confidence=0.0)
        else:
            proposal = _mock_plan(alert, ctx)

    # --- self-check: a proposed action MUST cite a retrieved runbook -----
    valid_sources = {r["source"] for r in ctx["runbooks"]}
    if proposal.tool_name and proposal.citation_source not in valid_sources:
        log("citation_rejected", proposed=proposal.tool_name, cited=proposal.citation_source)
        proposal.tool_name = None
        proposal.needs_human = True
        proposal.reasoning += " [rejected: action was not grounded in a runbook citation]"

    cost = estimate_cost(config.AGENT_MODEL, input_tokens, output_tokens)
    trace = {
        "alert": alert.get("name"), "service": ctx["service"],
        "tool_name": proposal.tool_name, "needs_human": proposal.needs_human,
        "citation": proposal.citation_source, "latency_ms": t.ms,
        "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost,
        "mode": "claude" if config.use_real_llm() else "mock",
    }
    log("plan_complete", **trace)
    return {"proposal": proposal.model_dump(), "context": ctx, "trace": trace}
