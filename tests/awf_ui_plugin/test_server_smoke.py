"""Smoke test: server starts and registers all 5 tools."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_workflow_ui.config import load, ensure_directories
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
    set_http_port(13747)
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
    """All 5 tools are registered with correct names."""
    server = create_server()
    tools = asyncio.run(server.list_tools())

    tool_names = {t.name for t in tools}
    expected = {
        "open_form",
        "open_form_and_wait",
        "read_submit",
        "wait_for_submit",
        "cancel_form",
        "list_pending_forms",
        "list_templates",
    }
    assert tool_names == expected, f"Missing tools: {expected - tool_names}"


def test_open_form_stub_returns_form_id(plugin_initialized):
    """open_form returns a form_id and submit_url."""
    server = create_server()
    result = asyncio.run(server.call_tool("open_form", {"template": "role-assignment", "data": {"available_roles": ["worker"]}}))
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
