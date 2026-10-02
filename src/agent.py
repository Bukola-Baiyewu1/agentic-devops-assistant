"""The agent core: observe -> retrieve -> plan (bounded loop) -> verify.

The planner only *proposes*. `policy.validate_proposal` then checks the tool,
arguments, target service, replica policy, citation, and confidence. Any
failure turns the proposal into an escalation, and the reasons are recorded.
Execution happens elsewhere, and only after a human approves.
"""

from __future__ import annotations

from typing import Any

from .config import settings
from .demo import planner_faults
from .observability import (
    LLM_COST,
    LLM_TOKENS,
    PLAN_LATENCY,
    PLANS,
    UNSAFE_REJECTED,
    Timer,
    estimate_cost,
    get_correlation_id,
    log,
)
from .planners import (
    PermanentPlannerError,
    PlanningContext,
    PlanResult,
    TransientPlannerError,
    get_planner,
)
from .policy import Proposal, service_allowed, validate_proposal
from .rag import get_retriever
from .tools import get_recent_logs, get_service_health
from .tracing import get_tracer


def build_context(alert: dict) -> PlanningContext:
    service = alert.get("service", "web")
    query = f"{alert.get('name', '')} {alert.get('description', '')}".strip()
    allowed = service_allowed(service)
    retrieved = get_retriever().retrieve(query) if query else []
    return PlanningContext(
        alert=alert,
        service=service,
        health=get_service_health(service) if allowed else None,
        logs=get_recent_logs(service, 20) if allowed else "",
        retrieved={r.chunk.chunk_id: r.chunk for r in retrieved},
        retrieval_scores={r.chunk.chunk_id: r.score for r in retrieved},
    )


def _short_reason(reason: str) -> str:
    return reason.split(" (")[0].split("'")[0].strip()[:60]


def plan(alert: dict, planner: Any | None = None) -> dict:
    """Run the full pipeline for one alert and return a validated plan.

    Raises TransientPlannerError when the planner hit a temporary failure, so
    the event queue can retry with backoff.
    """
    planner = planner or get_planner()
    tracer = get_tracer()

    with tracer.observe("triage", as_type="agent", input={"alert": alert}) as root:
        with tracer.observe("observe-and-retrieve", as_type="retriever") as obs:
            ctx = build_context(alert)
            obs.update(
                output={
                    "health": ctx.health,
                    "retrieved": [{"chunk_id": k, "score": v} for k, v in ctx.retrieval_scores.items()],
                }
            )

        validation_reasons: list[str] = []
        with Timer() as t:
            fault = planner_faults.take()
            if fault == "transient":
                raise TransientPlannerError("injected transient planner fault (demo)")
            try:
                if fault == "permanent":
                    raise PermanentPlannerError("injected permanent planner fault (demo)")
                result: PlanResult = planner.plan(ctx)
            except PermanentPlannerError as exc:
                log("planner_permanent_error", error=str(exc))
                result = PlanResult(
                    Proposal(
                        decision="escalate",
                        reasoning=f"Planner failed permanently ({exc}); escalating.",
                        confidence=0.0,
                    ),
                    mode=planner.mode,
                )

        with tracer.observe("verify-proposal", as_type="guardrail") as guard:
            proposal = result.proposal
            validation_reasons = validate_proposal(
                proposal, alert_service=ctx.service, retrieved=ctx.retrieved, health=ctx.health
            )
            if validation_reasons:
                for reason in validation_reasons:
                    UNSAFE_REJECTED.labels(reason=_short_reason(reason)).inc()
                log("proposal_rejected", tool=proposal.tool_name, reasons=validation_reasons)
                proposal = Proposal(
                    decision="escalate",
                    reasoning=proposal.reasoning + " [rejected by policy: " + "; ".join(validation_reasons) + "]",
                    citation_chunk_id=proposal.citation_chunk_id,
                    confidence=proposal.confidence,
                )
            guard.update(output={"decision": proposal.decision, "rejected_reasons": validation_reasons})

        cited = ctx.retrieved.get(proposal.citation_chunk_id or "")
        citation = cited.citation() if cited else None
        cost = estimate_cost(result.input_tokens, result.output_tokens)
        decision = "rejected" if validation_reasons else proposal.decision

        PLANS.labels(decision=decision, mode=result.mode).inc()
        PLAN_LATENCY.observe(t.seconds)
        LLM_TOKENS.labels(direction="input").inc(result.input_tokens)
        LLM_TOKENS.labels(direction="output").inc(result.output_tokens)
        LLM_COST.inc(cost)

        trace = {
            "correlation_id": get_correlation_id(),
            "alert": alert.get("name"),
            "service": ctx.service,
            "mode": result.mode,
            "model": settings.agent_model if result.mode == "anthropic" else None,
            "decision": decision,
            "tool_name": proposal.tool_name,
            "citation": citation,
            "retrieved": [{"chunk_id": k, "score": v} for k, v in ctx.retrieval_scores.items()],
            "iterations": result.iterations,
            "read_tool_calls": result.tool_calls,
            "rejected_reasons": validation_reasons,
            "schema_failures": result.schema_failures,
            "latency_ms": t.ms,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": cost,
            "cost_is_estimate": True,
        }
        root.update(output={"decision": decision, "tool": proposal.tool_name, "citation": citation})
        log("plan_complete", **{k: v for k, v in trace.items() if k not in ("retrieved", "read_tool_calls")})

    return {
        "proposal": proposal.model_dump(),
        "citation": citation,
        "health_at_plan": ctx.health,
        "trace": trace,
    }
