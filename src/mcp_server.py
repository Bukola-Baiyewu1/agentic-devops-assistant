"""An MCP server exposing the same tools to any MCP client (Claude Code,
Claude Desktop, Cursor, ...).

Run it with:  python -m src.mcp_server

The built-in agent (agent.py) calls these tool functions directly for the
automated alert flow; this server makes them available for interactive use too,
which is where full MCP tool-calling shines.
"""
from fastmcp import FastMCP

from . import tools

mcp = FastMCP("aegis-devops")


@mcp.tool()
def get_service_health(service: str = "web") -> dict:
    """Return current status and key metrics for a service."""
    return tools.get_service_health(service)


@mcp.tool()
def get_recent_logs(service: str = "web", lines: int = 100) -> str:
    """Return the most recent log lines for a service."""
    return tools.get_recent_logs(service, lines)


@mcp.tool()
def search_runbooks(query: str) -> list:
    """Search the runbooks and return matching excerpts with their source file."""
    return tools.search_runbooks(query)


@mcp.tool()
def run_diagnostic(command: str) -> str:
    """Run a whitelisted, read-only diagnostic command."""
    return tools.run_diagnostic(command)


if __name__ == "__main__":
    mcp.run()
