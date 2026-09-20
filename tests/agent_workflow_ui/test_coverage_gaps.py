"""Tests for MCP tool error paths + awf_kill (coverage gap fix).

Targets uncovered lines in tools/awf.py (was 53% coverage).
Each test exercises the catch-all Exception handler or a new tool.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from awf import api


@pytest.fixture
def project(tmp_path):
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
    return repo


@pytest.fixture
def tmp_git_repo(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


class TestAwfKill:
    """awf_kill tool — 0% covered, needs tests."""

    def test_kill_no_running_pipeline(self, project):
        from agent_workflow_ui.tools.awf import awf_kill
        result = asyncio.run(awf_kill(project_dir=str(project)))
        assert result["status"] == "ok"
        assert result["killed"] is False

    def test_kill_dead_pid_in_state(self, project):
        from awf.pipeline_state import write_state
        write_state(project, pipeline_pid="999999", stage_name="agent-dev")
        from agent_workflow_ui.tools.awf import awf_kill
        result = asyncio.run(awf_kill(project_dir=str(project)))
        assert result["status"] == "ok"
        assert result["killed"] is False

    def test_kill_live_pid(self, project, monkeypatch):
        # Use python3 process; AUD04-07 strict identity — make it look
        # like an awf pipeline via the read_cmdline seam.
        proc = subprocess.Popen([__import__("sys").executable, "-c", "import time; time.sleep(300)"])
        from awf.api import _liveness
        from awf.pipeline_state import write_state
        write_state(project, pipeline_pid=str(proc.pid))
        monkeypatch.setattr(
            _liveness, "read_cmdline",
            lambda pid: "python\x00-m\x00awf\x00start\x00" if pid == proc.pid else None,
        )
        from agent_workflow_ui.tools.awf import awf_kill
        result = asyncio.run(awf_kill(project_dir=str(project)))
        assert result["status"] == "ok"
        assert result["killed"] is True
        assert result["pid"] == proc.pid

    def test_kill_missing_agentic(self, tmp_path):
        """No .agentic/ → no-op (killed=False), not error."""
        from agent_workflow_ui.tools.awf import awf_kill
        result = asyncio.run(awf_kill(project_dir=str(tmp_path / "empty")))
        assert result["status"] == "ok"
        assert result["killed"] is False


class TestCatchAllExceptionHandlers:
    """Test catch-all 'except Exception' handlers in MCP tools."""

    def test_status_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "get_status", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
        result = asyncio.run(awf_mod.awf_status(project_dir=str(project)))
        assert result["status"] == "error"
        assert "OSError" in result["error"]

    def test_start_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "start_pipeline", lambda *a, **kw: (_ for _ in ()).throw(ValueError("bad")))
        monkeypatch.setattr(awf_mod, "_resolve_project_dir", lambda x: Path(project))
        result = asyncio.run(awf_mod.awf_start(project_dir=str(project)))
        assert result["status"] == "error"

    def test_continue_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "continue_pipeline", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash")))
        result = asyncio.run(awf_mod.awf_continue(project_dir=str(project)))
        assert result["status"] == "error"

    def test_baseline_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "create_baseline", lambda *a, **kw: (_ for _ in ()).throw(OSError("denied")))
        result = asyncio.run(awf_mod.awf_baseline(todo_id="TODO-0001", project_dir=str(project)))
        assert result["status"] == "error"

    def test_rollback_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "rollback", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("git")))
        result = asyncio.run(awf_mod.awf_rollback(todo_id="TODO-0001", project_dir=str(project)))
        assert result["status"] == "error"

    def test_approve_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "approve_commit", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk")))
        result = asyncio.run(awf_mod.awf_approve(todo_id="TODO-0001", project_dir=str(project)))
        assert result["status"] == "error"

    def test_report_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "get_report", lambda *a, **kw: (_ for _ in ()).throw(ValueError("parse")))
        result = asyncio.run(awf_mod.awf_report(project_dir=str(project)))
        assert result["status"] == "error"

    def test_reset_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "reset_runtime", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("cleanup")))
        result = asyncio.run(awf_mod.awf_reset(project_dir=str(project)))
        assert result["status"] == "error"

    def test_dispatch_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "dispatch_todo", lambda *a, **kw: (_ for _ in ()).throw(OSError("write")))
        result = asyncio.run(awf_mod.awf_dispatch_todo(content="# Test", project_dir=str(project)))
        assert result["status"] == "error"

    def test_load_context_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "load_supervisor_context", lambda *a, **kw: (_ for _ in ()).throw(OSError("read")))
        result = asyncio.run(awf_mod.awf_load_supervisor_context(project_dir=str(project)))
        assert result["status"] == "error"

    def test_kill_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "kill_pipeline", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("signal")))
        result = asyncio.run(awf_mod.awf_kill(project_dir=str(project)))
        assert result["status"] == "error"

    def test_wait_event_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "wait_for_event", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("poll")))
        result = asyncio.run(awf_mod.awf_wait_for_event(project_dir=str(project), timeout=1))
        assert result["status"] == "error"

    def test_model_config_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "check_model_config", lambda *a, **kw: (_ for _ in ()).throw(OSError("config")))
        result = asyncio.run(awf_mod.awf_check_model_config(project_dir=str(project)))
        assert result["status"] == "error"

    def test_add_role_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "add_role", lambda *a, **kw: (_ for _ in ()).throw(OSError("template")))
        result = asyncio.run(awf_mod.awf_add_role(name="qa", project_dir=str(project)))
        assert result["status"] == "error"

    def test_analyze_roles_unexpected(self, project, monkeypatch):
        from agent_workflow_ui.tools import awf as awf_mod
        monkeypatch.setattr(awf_mod.api, "analyze_roles", lambda *a, **kw: (_ for _ in ()).throw(ValueError("zone")))
        result = asyncio.run(awf_mod.awf_analyze_roles(project_dir=str(project)))
        assert result["status"] == "error"
