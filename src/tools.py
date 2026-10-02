"""The agent's capabilities.

READ tools are always allowed. ACTION tools refuse to run without a valid
approval token — that is the human-in-the-loop guardrail, enforced at the tool
level (defense in depth) so no code path can act without approval.
"""
from typing import List, Dict

from .demo import sim
from .rag import rag


class ApprovalError(Exception):
    """Raised when an action is attempted without a valid approval token."""


# In-memory set of tokens that have been issued and not yet spent.
_valid_tokens = set()


def issue_token(token: str):
    _valid_tokens.add(token)


def _spend(token: str):
    if token not in _valid_tokens:
        raise ApprovalError("missing or invalid approval token — refusing to act")
    _valid_tokens.discard(token)  # single use


# ---- READ tools (no approval needed) --------------------------------
def get_service_health(service: str = "web") -> dict:
    return sim.health()


def get_recent_logs(service: str = "web", lines: int = 100) -> str:
    return sim.logs(lines)


def search_runbooks(query: str) -> List[Dict]:
    return rag.retrieve(query)


ALLOWED_DIAGNOSTICS = {"uptime", "free", "df", "ps"}


def run_diagnostic(command: str) -> str:
    base = command.strip().split(" ")[0]
    if base not in ALLOWED_DIAGNOSTICS:
        return f"refused: '{base}' is not in the read-only allowlist {sorted(ALLOWED_DIAGNOSTICS)}"
    return f"[diagnostic:{base}] ok (simulated)"


# ---- ACTION tools (require approval_token) --------------------------
def restart_service(service: str, approval_token: str) -> dict:
    _spend(approval_token)
    sim.restart()
    return {"action": "restart_service", "service": service, "result": sim.health()}


def scale_service(service: str, replicas: int, approval_token: str) -> dict:
    _spend(approval_token)
    sim.scale(replicas)
    return {"action": "scale_service", "service": service, "replicas": replicas, "result": sim.health()}


# Maps the tool names the planner may propose to their implementations.
ACTION_TOOLS = {
    "restart_service": restart_service,
    "scale_service": scale_service,
}
