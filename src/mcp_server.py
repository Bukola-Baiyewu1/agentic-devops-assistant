"""MCP server: the standard tool interface for Aegis.

Run it with:  python -m src.mcp_server   (stdio transport)

Any MCP client (Claude Code, Claude Desktop, Cursor, ...) can call the
read-only tools freely. The action tools are exposed too, but MCP is only the
interface, not the authority: every action tool requires an execution
capability that the Aegis server mints after a human approves that exact
action (POST /actions/{id}/approve with "execute": false). The capability is
bound to the action ID, tool, normalized arguments, target service, approver,
expiry, and a nonce, and it works once.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import approval, tools
from .security import ApprovalError
from .state import ConflictError
from .states import InvalidTransition

mcp = FastMCP(
    "aegis-devops",
    instructions=(
        "Read tools are safe to call. Action tools change the simulated service and require a "
        "single-use capability issued by a human approver for that exact action."
    ),
)


def _guard(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (ApprovalError, approval.InvalidApprovalToken) as e:
        raise ToolError(f"refused: {e}") from e
    except (InvalidTransition, ConflictError) as e:
        raise ToolError(f"refused: {e}") from e
    except KeyError as e:
        raise ToolError(f"not found: {e}") from e
    except ValueError as e:
        raise ToolError(f"invalid request: {e}") from e


# ---- read tools -----------------------------------------------------------
@mcp.tool(annotations={"readOnlyHint": True})
def get_service_health(service: str = "web") -> dict:
    """Return current status and key metrics for a managed service."""
    return _guard(lambda: tools.get_service_health(service))


@mcp.tool(annotations={"readOnlyHint": True})
def get_recent_logs(service: str = "web", lines: int = 50) -> str:
    """Return the most recent log lines (1-200) for a managed service."""
    return _guard(lambda: tools.get_recent_logs(service, lines))


@mcp.tool(annotations={"readOnlyHint": True})
def search_runbooks(query: str) -> list[dict]:
    """Search the runbooks; each result has a chunk_id, source file, heading, and line range."""
    return _guard(lambda: tools.search_runbooks(query))


@mcp.tool(annotations={"readOnlyHint": True})
def run_diagnostic(command: str) -> str:
    """Run one argument-free, read-only diagnostic: uptime, free, df, or ps."""
    return tools.run_diagnostic(command)


@mcp.tool(annotations={"readOnlyHint": True})
def get_action(action_id: str) -> dict:
    """Show an action's proposal, citation, status, and audit fields (never secrets)."""
    return _guard(lambda: approval.public_view(approval._get(action_id)))


# ---- action tools (approval capability required) ---------------------------
@mcp.tool(annotations={"destructiveHint": True})
def restart_service(service: str, action_id: str, capability: str) -> dict:
    """Restart a service. Requires the capability issued when a human approved this action."""
    return _guard(
        lambda: approval.public_view(
            approval.execute_approved(
                action_id, tool="restart_service", args={"service": service}, capability=capability, actor="mcp"
            )
        )
    )


@mcp.tool(annotations={"destructiveHint": True})
def scale_service(service: str, replicas: int, action_id: str, capability: str) -> dict:
    """Scale a service. Requires the capability issued when a human approved this exact replica count."""
    return _guard(
        lambda: approval.public_view(
            approval.execute_approved(
                action_id,
                tool="scale_service",
                args={"service": service, "replicas": replicas},
                capability=capability,
                actor="mcp",
            )
        )
    )


@mcp.tool(annotations={"destructiveHint": True})
def rollback(action_id: str, capability: str) -> dict:
    """Restore the state recorded before an action. Requires a separately approved rollback capability."""
    return _guard(
        lambda: approval.public_view(approval.execute_approved_rollback(action_id, capability=capability, actor="mcp"))
    )


if __name__ == "__main__":
    mcp.run()
