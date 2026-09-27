"""FastMCP server: registers tools and runs stdio transport."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import registry


def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all tools registered.

    R-06 (TODO-0112): the registration list lives in ``tools/registry.py``
    (``registry.TOOLS``) — the single source for the server and the
    docs count (A-08). Each entry is a thin async wrapper over awf.api.*
    functions; see tools/awf.py for docstrings and parameter contracts.
    """
    mcp = FastMCP(
        "agent-workflow-ui",
        instructions=(
            "Plugin for visual interaction (HTML forms, dashboards) "
            "between opencode agents and users, plus awf workflow operations "
            "(init, status, start, baseline, rollback, approve, report, "
            "reset, add-role, analyze-roles)."
        ),
    )

    for spec in registry.TOOLS:
        mcp.add_tool(spec.fn, name=spec.name)

    return mcp
