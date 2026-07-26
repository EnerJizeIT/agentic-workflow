"""FastMCP server: registers tools and runs stdio transport."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import forms, templates


def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all tools registered."""
    mcp = FastMCP(
        "agent-workflow-ui",
        instructions=(
            "Plugin for visual interaction (HTML forms, dashboards) "
            "between opencode agents and users."
        ),
    )

    mcp.add_tool(forms.open_form, name="open_form")
    mcp.add_tool(forms.open_form_and_wait, name="open_form_and_wait")
    mcp.add_tool(forms.read_submit, name="read_submit")
    mcp.add_tool(forms.wait_for_submit, name="wait_for_submit")
    mcp.add_tool(forms.cancel_form, name="cancel_form")
    mcp.add_tool(forms.list_pending_forms, name="list_pending_forms")

    mcp.add_tool(templates.list_templates, name="list_templates")

    return mcp
