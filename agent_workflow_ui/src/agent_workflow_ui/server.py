"""FastMCP server: registers tools and runs stdio transport."""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .tools import awf, forms, templates


def create_server() -> FastMCP:
    """Create and configure the FastMCP server with all tools registered."""
    mcp = FastMCP(
        "agent-workflow-ui",
        instructions=(
            "Plugin for visual interaction (HTML forms, dashboards) "
            "between opencode agents and users, plus awf workflow operations "
            "(init, status, start, baseline, rollback, approve, report, "
            "reset, add-role, analyze-roles)."
        ),
    )

    # ── UI tools: form lifecycle ─────────────────────────────────────────
    mcp.add_tool(forms.open_form, name="open_form")
    mcp.add_tool(forms.read_submit, name="read_submit")
    mcp.add_tool(forms.cancel_form, name="cancel_form")
    mcp.add_tool(forms.list_pending_forms, name="list_pending_forms")

    # ── UI tools: template discovery ─────────────────────────────────────
    mcp.add_tool(templates.list_templates, name="list_templates")

    # ── awf workflow tools (MCP-3) ───────────────────────────────────────
    # Each is a thin async wrapper over awf.api.* functions. See
    # tools/awf.py for docstrings and parameter contracts.
    mcp.add_tool(awf.awf_init, name="awf_init")
    mcp.add_tool(awf.awf_status, name="awf_status")
    mcp.add_tool(awf.awf_start, name="awf_start")
    mcp.add_tool(awf.awf_continue, name="awf_continue")
    mcp.add_tool(awf.awf_baseline, name="awf_baseline")
    mcp.add_tool(awf.awf_rollback, name="awf_rollback")
    mcp.add_tool(awf.awf_approve, name="awf_approve")
    mcp.add_tool(awf.awf_report, name="awf_report")
    mcp.add_tool(awf.awf_reset, name="awf_reset")
    mcp.add_tool(awf.awf_add_role, name="awf_add_role")
    mcp.add_tool(awf.awf_analyze_roles, name="awf_analyze_roles")
    # Dogfood-2 automation: dispatch + context
    mcp.add_tool(awf.awf_dispatch_todo, name="awf_dispatch_todo")
    mcp.add_tool(awf.awf_load_supervisor_context, name="awf_load_supervisor_context")
    # Dogfood-5: shortcut tools (deterministic UX, no prompt-only guidance)
    mcp.add_tool(awf.awf_open_project_setup_form, name="awf_open_project_setup_form")
    # Dogfood-7: increment planning (user picks decomposition variant)
    mcp.add_tool(awf.awf_open_increment_planning_form, name="awf_open_increment_planning_form")
    # DASH Phase 2: pipeline dashboard
    mcp.add_tool(awf.awf_open_pipeline_dashboard, name="awf_open_pipeline_dashboard")
    # DASH Phase 3: supervisor wake-up (no more polling)
    mcp.add_tool(awf.awf_wait_for_event, name="awf_wait_for_event")
    # Model configuration validation (dogfood-10)
    mcp.add_tool(awf.awf_check_model_config, name="awf_check_model_config")

    return mcp
