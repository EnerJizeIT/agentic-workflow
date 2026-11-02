"""T16: удалённые MCP-имена дают свой отказ, Python-API снят.

IMP1: ``awf_current_step`` и ``awf_load_supervisor_context`` удалены.
Их нет в реестре/сервере, вызов старого имени получает человеческий
текст с подсказкой на ``awf_brief`` (не ``Unknown tool``), а публичная
``awf.api.load_supervisor_context`` больше не импортируется. Helper'ы,
которыми пользуется ``get_status``, остаются (AC47).
"""
from __future__ import annotations

import asyncio

import pytest
from agent_workflow_ui.tools import registry

pytest.importorskip("mcp")
from agent_workflow_ui.server import create_server  # noqa: E402
from mcp.server.fastmcp.exceptions import ToolError  # noqa: E402

REMOVED = {
    "awf_current_step": "awf_brief",
    "awf_load_supervisor_context": "awf_brief",
}


def test_removed_tools_mapping_is_frozen() -> None:
    assert registry.REMOVED_TOOLS == REMOVED


def test_removed_names_are_not_registered() -> None:
    names = set(registry.tool_names())
    assert names.isdisjoint(REMOVED)


def test_removed_name_gets_a_human_refusal() -> None:
    server = create_server()
    for name, replacement in REMOVED.items():
        with pytest.raises(ToolError) as exc:
            asyncio.run(server.call_tool(name, {}))
        text = str(exc.value)
        assert name in text
        assert "removed" in text or "удалён" in text
        assert replacement in text
        assert "Unknown tool" not in text


def test_other_unknown_name_keeps_the_standard_error() -> None:
    server = create_server()
    with pytest.raises(ToolError) as exc:
        asyncio.run(server.call_tool("awf_no_such_tool", {}))
    text = str(exc.value)
    assert "Unknown tool" in text
    assert "awf_brief" not in text


def test_public_python_api_is_gone() -> None:
    from awf import api

    assert not hasattr(api, "load_supervisor_context")
    assert not hasattr(api, "SupervisorContextResult")
    with pytest.raises(ImportError):
        from awf.api import load_supervisor_context  # noqa: F401
    with pytest.raises(ImportError):
        from awf.api import SupervisorContextResult  # noqa: F401


def test_status_helpers_stay_and_get_status_works(tmp_git_repo) -> None:
    from awf import api
    from awf.api.context import (
        _compute_expected_action,
        _compute_stage_kind,
        _extract_stage_info,
    )

    assert callable(_extract_stage_info)
    assert callable(_compute_expected_action)
    assert callable(_compute_stage_kind)

    api.init_project(tmp_git_repo, project_name="RemovalTest")
    status = api.get_status(tmp_git_repo)
    assert status.project_name == "RemovalTest"


# ── QA (TODO-0342 stage 4): the wire, not just the Python method ────────
#
# ``test_removed_name_gets_a_human_refusal`` calls ``server.call_tool``
# directly, so it stays green even if FastMCP stops routing the protocol
# request through that method (the message would never reach a client).
# These two go through a real client session over the in-memory transport.


async def _protocol_calls(project_dir: str) -> tuple[set[str], dict[str, str]]:
    from mcp.shared.memory import create_connected_server_and_client_session

    server = create_server()
    texts: dict[str, str] = {}
    async with create_connected_server_and_client_session(
        server._mcp_server
    ) as client:
        listed = await client.list_tools()
        for name in (*REMOVED, "awf_no_such_tool"):
            result = await client.call_tool(name, {})
            texts[name] = "".join(
                getattr(c, "text", "") for c in result.content
            )
        live = await client.call_tool("awf_brief", {"project_dir": project_dir})
        texts["awf_brief"] = "".join(
            getattr(c, "text", "") for c in live.content
        )
        assert live.isError is False, texts["awf_brief"]
        return {t.name for t in listed.tools}, texts


def test_protocol_call_of_removed_name_gets_the_refusal(tmp_git_repo) -> None:
    """AC10 on the real MCP path: the client sees our text, not silence.

    A green direct-method test does not prove the refusal reaches a caller;
    this one observes the payload the MCP client actually receives.
    """
    names, texts = asyncio.run(_protocol_calls(str(tmp_git_repo)))
    assert names.isdisjoint(REMOVED)
    assert len(names) == 54
    for name, replacement in REMOVED.items():
        text = texts[name]
        assert name in text
        assert "removed" in text or "удалён" in text
        assert replacement in text
        assert "Unknown tool" not in text
    assert "Unknown tool" in texts["awf_no_such_tool"]
    assert "awf_brief" not in texts["awf_no_such_tool"]


def test_cli_has_no_removed_subcommands(tmp_git_repo) -> None:
    """AC12: the two subcommands are gone; ``brief``/``status`` still work."""
    import subprocess
    import sys

    from awf import api

    api.init_project(tmp_git_repo, project_name="RemovalTest")
    for gone in ("current-step", "supervisor-context"):
        proc = subprocess.run(
            [sys.executable, "-m", "awf", gone],
            capture_output=True, text=True, check=False, timeout=120,
        )
        assert proc.returncode != 0
        assert "invalid choice" in proc.stderr
        assert "Traceback" not in proc.stderr
    for kept in ("brief", "status"):
        proc = subprocess.run(
            [
                sys.executable, "-m", "awf", kept,
                "--project-dir", str(tmp_git_repo),
            ],
            capture_output=True, text=True, check=False, timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Traceback" not in proc.stderr
