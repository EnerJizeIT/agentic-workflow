"""Smoke test: server starts and registers all 5 tools.

Requires the ``mcp`` package (provided by opencode at runtime). Skipped
in CI environments where ``mcp`` is not installed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

# Skip entire module if mcp isn't available (CI runners without opencode).
pytest.importorskip("mcp.server.fastmcp")

from agent_workflow_ui.config import ensure_directories, load
from agent_workflow_ui.http_endpoint import _find_free_port  # AUD12-09
from agent_workflow_ui.render.engine import create_env
from agent_workflow_ui.server import create_server
from agent_workflow_ui.state import (
    reset_registry,
    set_config,
    set_http_port,
    set_jinja_env,
)

import agent_workflow_ui as _awui

DEFAULT_TEMPLATES_DIR = Path(_awui.__file__).parent / "render" / "default_templates"


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global form registry before each test."""
    reset_registry()


@pytest.fixture
def plugin_initialized(tmp_path, monkeypatch):
    """Initialize plugin state so tools can run through the server.

    CRITICAL: monkeypatch browser.open_path to a no-op so tests don't actually
    open real browser tabs. Redirects temp_dir to tmp_path so HTML files
    don't leak into /tmp/.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AWF_TEMP_DIR", str(tmp_path / "tmp"))
    config = load()
    ensure_directories(config)
    set_config(config)
    # AUD12-09: no hardcoded port — the suite must not depend on a
    # specific port being free.
    set_http_port(_find_free_port())
    set_jinja_env(create_env([config.templates_dir, DEFAULT_TEMPLATES_DIR]))
    reset_registry()

    # Mock browser.open_path so tests are hermetic — don't open real browser.
    def _fake_open_path(target, command="auto"):
        return True, f"mocked open for {target}"

    import agent_workflow_ui.tools.forms as forms_mod
    monkeypatch.setattr(forms_mod, "open_path", _fake_open_path, raising=True)
    import agent_workflow_ui.browser as browser_mod
    monkeypatch.setattr(browser_mod, "open_path", _fake_open_path, raising=True)

    return config


def test_server_creates():
    """Server instance creates without errors."""
    server = create_server()
    assert server.name == "agent-workflow-ui"


def test_all_tools_registered():
    """All 40 tools are registered with correct names (5 UI + 35 awf).

    AUD08-08: this number is a fact-check, not a label — if it drifts from
    ``server.py``, ``tool_names == expected`` below fails first. Update both
    together when a tool is added.
    """
    server = create_server()
    tools = asyncio.run(server.list_tools())

    tool_names = {t.name for t in tools}
    expected = {
        # UI tools (form lifecycle + templates)
        "open_form",
        "read_submit",
        "cancel_form",
        "list_pending_forms",
        "list_templates",
        # awf workflow tools (MCP-3)
        "awf_init",
        "awf_status",
        "awf_start",
        "awf_continue",
        "awf_retry_stage",
        "awf_baseline",
        "awf_rollback",
        "awf_approve",
        "awf_reject",
        "awf_report",
        "awf_reset",
        "awf_add_role",
        "awf_analyze_roles",
        # Dogfood-2 automation
        "awf_dispatch_todo",
        "awf_load_supervisor_context",
        # Dogfood-5: shortcut tools
        "awf_open_project_setup_form",
        # Dogfood-7: increment planning
        "awf_open_increment_planning_form",
        # DASH Phase 2: pipeline dashboard
        "awf_open_pipeline_dashboard",
        # DASH Phase 3: supervisor wake-up
        "awf_wait_for_event",
        # Model configuration validation (dogfood-10)
        "awf_check_model_config",
        "awf_kill",
        # SMO: phase-aware supervisor tools
        "awf_current_step",
        "awf_set_goal",
        "awf_confirm_normalized",
        # SPEC A-run: autonomous run (забег)
        "awf_run_start",
        "awf_run_status",
        "awf_run_next",
        "awf_run_finish",
        "awf_run_note",
        "awf_restore",
        # RUN3 #4/#5: state hygiene (stale closures, never-started removal)
        "awf_unblock",
        "awf_todo_remove",
        # U4: machine proof that tests are red on the baseline sha
        "awf_prove_red",
        # U5: one deterministic verify report (GATES-<todo>.md)
        "awf_verify_pack",
        # U8: token/cost metrics of the work program (report to desktop)
        "awf_metrics",
    }
    assert tool_names == expected, f"Missing tools: {expected - tool_names}"


def test_open_form_stub_returns_form_id(plugin_initialized):
    """open_form returns a form_id and submit_url."""
    server = create_server()
    result = asyncio.run(server.call_tool("open_form", {"template": "project-setup", "data": {"available_roles": ["worker"]}}))
    assert result is not None
    _, structured = result
    assert "form_id" in structured
    assert "submit_url" in structured


def test_list_pending_returns_empty():
    """list_pending_forms returns empty list initially."""
    server = create_server()
    result = asyncio.run(server.call_tool("list_pending_forms", {}))
    assert result is not None
    _, structured = result
    assert structured["count"] == 0
    assert structured["pending"] == []
