"""Test: MCP event loop not blocked by wait_for_event (DF5-12 regression).

Validates that asyncio.to_thread wrapper in awf_wait_for_event keeps
the event loop free for other tools. Without the fix, time.sleep()
inside wait_for_event blocked ALL other MCP tool calls.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from awf import api


@pytest.fixture
def project(tmp_path):
    """Git repo with .agentic/ structure for testing."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    api.init_project(repo, project_name="Test")
    from awf.pipeline_state import write_state

    write_state(repo, stage_name="agent-dev", stage_kind="execute", todo_id="TODO-0001")
    return repo


async def _run_concurrent_status_while_waiting(project_dir: str):
    """Start wait_for_event, then call awf_status while it blocks."""
    from agent_workflow_ui.tools.awf import awf_status, awf_wait_for_event

    wait_task = asyncio.create_task(
        awf_wait_for_event(project_dir=project_dir, timeout=5)
    )
    await asyncio.sleep(0.5)

    status_start = time.monotonic()
    status_result = await awf_status(project_dir=project_dir)
    status_elapsed = time.monotonic() - status_start

    wait_task.cancel()
    try:
        await wait_task
    except asyncio.CancelledError:
        pass

    return status_result, status_elapsed


async def _run_multiple_concurrent(project_dir: str):
    """Run 3 status calls concurrently while wait_for_event blocks."""
    from agent_workflow_ui.tools.awf import awf_status, awf_wait_for_event

    wait_task = asyncio.create_task(
        awf_wait_for_event(project_dir=project_dir, timeout=5)
    )
    await asyncio.sleep(0.5)

    results = await asyncio.gather(
        awf_status(project_dir=project_dir),
        awf_status(project_dir=project_dir),
        awf_status(project_dir=project_dir),
    )

    wait_task.cancel()
    try:
        await wait_task
    except asyncio.CancelledError:
        pass

    return results


class TestEventLoopNotBlocked:
    """DF5-12: asyncio.to_thread prevents event loop blocking."""

    def test_status_runs_while_wait_for_event_blocks(self, project):
        """awf_status completes while wait_for_event is still running.

        If event loop is blocked, awf_status hangs until wait_for_event
        times out (5s). This test catches that regression.
        """
        status_result, status_elapsed = asyncio.run(
            _run_concurrent_status_while_waiting(str(project))
        )

        assert status_result["status"] == "ok"
        assert status_elapsed < 2.0, (
            f"awf_status took {status_elapsed:.1f}s — event loop was blocked! "
            f"Check asyncio.to_thread in awf_wait_for_event wrapper."
        )

    def test_multiple_tools_concurrent_with_wait(self, project):
        """Multiple quick tools run concurrently while wait_for_event blocks."""
        results = asyncio.run(_run_multiple_concurrent(str(project)))
        assert all(r["status"] == "ok" for r in results)

    def test_wait_for_event_returns_timeout(self, project):
        """wait_for_event still functions correctly (returns event)."""
        from agent_workflow_ui.tools.awf import awf_wait_for_event

        result = asyncio.run(awf_wait_for_event(project_dir=str(project), timeout=3))
        assert result["status"] == "ok"
        assert result["event_type"] in ("timeout", "idle", "verify", "done")
