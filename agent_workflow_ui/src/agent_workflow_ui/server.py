"""FastMCP server: registers tools and runs stdio transport."""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .tools import registry


class RemovalAwareFastMCP(FastMCP):
    """FastMCP, который помнит снятые имена.

    Удалённые тулы не входят в ``registry.TOOLS`` (их нет в ``list_tools``
    и счётчиках), но вызов старого имени получает человеческий отказ с
    заменой, а не ``Unknown tool`` (AC10).
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        replacement = registry.REMOVED_TOOLS.get(name)
        if replacement:
            raise ToolError(
                f"Tool `{name}` is removed (удалён). Use `{replacement}` "
                "— the single entry point."
            )
        return await super().call_tool(name, arguments)


def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all tools registered.

    R-06 (TODO-0112): the registration list lives in ``tools/registry.py``
    (``registry.TOOLS``) — the single source for the server and the
    docs count (A-08). Each entry is a thin async wrapper over awf.api.*
    functions; see tools/awf.py for docstrings and parameter contracts.
    """
    mcp = RemovalAwareFastMCP(
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
