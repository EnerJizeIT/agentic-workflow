"""Smoke test: server starts and registers all 5 tools."""
from __future__ import annotations

import asyncio
import pytest

from agent_workflow_ui.server import create_server
from agent_workflow_ui.state import reset_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    """Reset the global form registry before each test."""
    reset_registry()


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
        "read_submit",
        "cancel_form",
        "list_pending_forms",
        "list_templates",
    }
    assert tool_names == expected, f"Missing tools: {expected - tool_names}"


def test_open_form_stub_returns_form_id():
    """open_form stub returns a form_id and not-implemented error."""
    server = create_server()
    result = asyncio.run(server.call_tool("open_form", {"template": "test"}))
    assert result is not None
    _, structured = result
    assert "form_id" in structured


def test_list_pending_returns_empty():
    """list_pending_forms returns empty list initially."""
    server = create_server()
    result = asyncio.run(server.call_tool("list_pending_forms", {}))
    assert result is not None
    _, structured = result
    assert structured["count"] == 0
    assert structured["pending"] == []
