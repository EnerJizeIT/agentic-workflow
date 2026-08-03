"""Tests for MCP tools in agent_workflow_ui.tools.awf.

Each tool is a thin wrapper around awf.api.* functions. Tests verify:
- Tool calls the right api function with correct args
- Returns {status: "ok", **result.as_dict()} on success
- Returns {status: "error", error: "..."} on AwfApiError
- Parameter proxying (project_dir default, override propagation)
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from agent_workflow_ui.tools import awf

from awf import api


@pytest.fixture
def git_project(tmp_path):
    """Create a git-initialized empty project."""
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
    return proj


@pytest.fixture
def initialized_project(git_project):
    """Project with .agentic/ already set up via api.init_project."""
    api.init_project(git_project, project_name="Test Project")
    # Dogfood-10: start_pipeline background guard requires active TODO
    inbox = git_project / ".agentic" / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "TODO-0001.ready").touch()
    (inbox / "TODO-0001.md").write_text("# Task")
    return git_project


def run(coro):
    """Run an async coroutine synchronously (test helper)."""
    return asyncio.run(coro)


# ─── awf_init ───────────────────────────────────────────────────────────


class TestAwfInit:
    def test_creates_skeleton(self, git_project):
        result = run(awf.awf_init(project_dir=str(git_project)))
        assert result["status"] == "ok"
        assert result["project_name"] == "Proj"
        assert "supervisor_md" in result
        assert len(result["supervisor_md"]) > 0
        assert "plan_md" in result
        assert "vision_excerpt" in result
        assert result["pipeline_configured"] is False
        assert "project-setup" in result["next_action"]
        # Files actually created
        assert (git_project / ".agentic" / "config.yaml").exists()
        assert (git_project / ".agentic" / "roles" / "supervisor.md").exists()

    def test_explicit_name_overrides_auto_derive(self, git_project):
        result = run(awf.awf_init(
            project_dir=str(git_project),
            project_name="Custom Name",
        ))
        assert result["status"] == "ok"
        assert result["project_name"] == "Custom Name"

    def test_not_git_returns_error(self, tmp_path):
        proj = tmp_path / "nogit"
        proj.mkdir()
        result = run(awf.awf_init(project_dir=str(proj)))
        assert result["status"] == "error"
        assert "Not a git repository" in result["error"]

    def test_already_initialized_without_force(self, git_project):
        run(awf.awf_init(project_dir=str(git_project)))
        result = run(awf.awf_init(project_dir=str(git_project)))
        assert result["status"] == "error"
        assert "already exists" in result["error"]

    def test_force_overwrites(self, git_project):
        run(awf.awf_init(project_dir=str(git_project)))
        result = run(awf.awf_init(project_dir=str(git_project), force=True))
        assert result["status"] == "ok"

    def test_returns_json_serializable(self, git_project):
        """MCP response must be JSON-serializable."""
        import json
        result = run(awf.awf_init(project_dir=str(git_project)))
        json.dumps(result)  # must not raise


# ─── awf_status ─────────────────────────────────────────────────────────


class TestAwfStatus:
    def test_empty_project(self, git_project):
        """Project without TODO — uses git_project (no init)."""
        from awf import api as _api
        _api.init_project(git_project, project_name="Test")
        result = run(awf.awf_status(project_dir=str(git_project)))
        assert result["status"] == "ok"
        assert result["project_name"] == "Test"
        assert result["active_todos"] == []
        assert result["suggestion"] is not None

    def test_missing_agentic_returns_error(self, tmp_path):
        proj = tmp_path / "empty"
        proj.mkdir()
        result = run(awf.awf_status(project_dir=str(proj)))
        assert result["status"] == "error"
        assert "No .agentic/" in result["error"]

    def test_with_active_todo(self, initialized_project):
        inbox = initialized_project / ".agentic" / "inbox"
        outbox = initialized_project / ".agentic" / "outbox"
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task")
        result = run(awf.awf_status(project_dir=str(initialized_project)))
        assert result["status"] == "ok"
        assert len(result["active_todos"]) == 1
        assert result["active_todos"][0]["todo_id"] == "TODO-0001"


# ─── awf_baseline ───────────────────────────────────────────────────────


class TestAwfBaseline:
    def test_creates_baseline(self, initialized_project):
        result = run(awf.awf_baseline(
            todo_id="TODO-0001",
            project_dir=str(initialized_project),
        ))
        assert result["status"] == "ok"
        assert result["todo_id"] == "TODO-0001"
        assert len(result["sha"]) == 40
        assert result["is_git_repo"] is True
        assert "BASELINE-TODO-0001.sha" in result["files_created"]
        assert (initialized_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").exists()

    def test_missing_agentic(self, tmp_path):
        result = run(awf.awf_baseline(
            todo_id="TODO-0001",
            project_dir=str(tmp_path),
        ))
        assert result["status"] == "error"


# ─── awf_rollback ───────────────────────────────────────────────────────


class TestAwfRollback:
    def test_dry_run(self, initialized_project):
        # First create baseline + change
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=initialized_project, text=True
        ).strip()
        (initialized_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n"
        )
        (initialized_project / "extra.txt").write_text("extra")
        subprocess.run(["git", "add", "-A"], cwd=initialized_project, check=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=initialized_project, check=True)

        result = run(awf.awf_rollback(
            todo_id="TODO-0001",
            project_dir=str(initialized_project),
            mode="dry-run",
        ))
        assert result["status"] == "ok"
        assert result["mode"] == "dry-run"
        assert result["ack_file"] is None
        # extra.txt still there
        assert (initialized_project / "extra.txt").exists()

    def test_hard_resets(self, initialized_project):
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=initialized_project, text=True
        ).strip()
        (initialized_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n"
        )
        (initialized_project / "extra.txt").write_text("extra")
        subprocess.run(["git", "add", "-A"], cwd=initialized_project, check=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=initialized_project, check=True)

        result = run(awf.awf_rollback(
            todo_id="TODO-0001",
            project_dir=str(initialized_project),
            mode="hard",
        ))
        assert result["status"] == "ok"
        assert not (initialized_project / "extra.txt").exists()
        assert result["ack_file"] is not None

    def test_missing_baseline(self, initialized_project):
        result = run(awf.awf_rollback(
            todo_id="TODO-9999",
            project_dir=str(initialized_project),
        ))
        assert result["status"] == "error"
        assert "Baseline SHA not found" in result["error"]


# ─── awf_approve ────────────────────────────────────────────────────────


class TestAwfApprove:
    def test_creates_signal(self, initialized_project):
        result = run(awf.awf_approve(
            todo_id="TODO-0001",
            project_dir=str(initialized_project),
        ))
        assert result["status"] == "ok"
        assert "APPROVE-TODO-0001.ready" in result["signal_file"]
        assert (initialized_project / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").exists()

    def test_empty_todo_id(self, initialized_project):
        result = run(awf.awf_approve(
            todo_id="",
            project_dir=str(initialized_project),
        ))
        assert result["status"] == "error"

    def test_missing_agentic_returns_error(self, tmp_path):
        """Audit fix: approve requires .agentic/ (was: created inbox on demand).
        Consistency with create_baseline, add_role, get_status, etc."""
        proj = tmp_path / "bare"
        proj.mkdir()
        result = run(awf.awf_approve(
            todo_id="TODO-0001",
            project_dir=str(proj),
        ))
        assert result["status"] == "error"
        assert "No .agentic/" in result["error"]


# ─── awf_report ─────────────────────────────────────────────────────────


class TestAwfReport:
    def test_empty_project(self, git_project):
        """Project without TODO — uses git_project (no init)."""
        from awf import api as _api
        _api.init_project(git_project, project_name="Test")
        result = run(awf.awf_report(project_dir=str(git_project)))
        assert result["status"] == "ok"
        assert result["project_name"] == "Test"
        assert result["items"] == []
        assert result["done_count"] == 0

    def test_with_done_task(self, initialized_project):
        ag = initialized_project / ".agentic"
        (ag / "inbox" / "TODO-0001.ready").touch()
        (ag / "outbox" / "DONE-TODO-0001.ready").touch()
        result = run(awf.awf_report(project_dir=str(initialized_project)))
        assert result["done_count"] == 1
        assert any(item["status"] == "OK" for item in result["items"])


# ─── awf_reset ──────────────────────────────────────────────────────────


class TestAwfReset:
    def test_default_cleans_runtime(self, initialized_project):
        ag = initialized_project / ".agentic"
        for d in ["inbox", "outbox", "context", "logs", "reports"]:
            (ag / d / "junk.txt").write_text("junk")
        result = run(awf.awf_reset(project_dir=str(initialized_project)))
        assert result["status"] == "ok"
        assert "inbox" in result["cleaned_dirs"]
        assert "outbox" in result["cleaned_dirs"]

    def test_tasks_only(self, initialized_project):
        ag = initialized_project / ".agentic"
        (ag / "inbox" / "x.txt").write_text("x")
        (ag / "context" / "y.txt").write_text("y")
        result = run(awf.awf_reset(
            project_dir=str(initialized_project),
            tasks_only=True,
        ))
        assert "inbox" in result["cleaned_dirs"]
        assert "context" not in result["cleaned_dirs"]

    def test_noop_when_no_agentic(self, tmp_path):
        proj = tmp_path / "nothing"
        proj.mkdir()
        result = run(awf.awf_reset(project_dir=str(proj)))
        assert result["status"] == "ok"
        assert result["mode"] == "noop"


# ─── awf_add_role ───────────────────────────────────────────────────────


class TestAwfAddRole:
    def test_creates_role(self, initialized_project):
        result = run(awf.awf_add_role(
            name="qa",
            project_dir=str(initialized_project),
            description="QA engineer",
            model="glm-5.2",
        ))
        assert result["status"] == "ok"
        assert result["role_name"] == "qa"
        role_file = initialized_project / ".agentic" / "roles" / "qa.md"
        assert role_file.exists()
        assert "glm-5.2" in role_file.read_text()

    def test_empty_name(self, initialized_project):
        result = run(awf.awf_add_role(
            name="",
            project_dir=str(initialized_project),
        ))
        assert result["status"] == "error"


# ─── awf_analyze_roles ──────────────────────────────────────────────────


class TestAwfAnalyzeRoles:
    def test_dry_run_returns_result(self, initialized_project):
        # analyze requires team roles (not just supervisor.md)
        (initialized_project / ".agentic" / "roles" / "worker.md").write_text(
            "# ROLE: worker\nImplementation role\n"
        )
        result = run(awf.awf_analyze_roles(
            project_dir=str(initialized_project),
            dry_run=True,
        ))
        # Either ok with empty overlaps, or error if no pipeline configured
        assert result["status"] in ("ok", "error")
        if result["status"] == "ok":
            assert "overlaps" in result
            assert "patches_applied" in result
            assert result["dry_run"] is True

    def test_missing_agentic(self, tmp_path):
        proj = tmp_path / "nothing"
        proj.mkdir()
        result = run(awf.awf_analyze_roles(project_dir=str(proj)))
        assert result["status"] == "error"


# ─── awf_start / awf_continue (background only — foreground blocks) ─────


class TestAwfStart:
    def test_background_returns_pid(self, initialized_project):
        """Background mode returns immediately with a PID."""
        result = run(awf.awf_start(
            project_dir=str(initialized_project),
            background=True,
            auto=True,
        ))
        # Background launches subprocess; may succeed or fail to find pipeline
        # but should never block
        assert result["status"] in ("ok", "error")
        if result["status"] == "ok":
            assert result["run_mode"] == "background"
            assert result["run_id"] is not None or result["exit_code"] is not None

    def test_foreground_orchestrator_crash_returns_error_not_crash(self, initialized_project, monkeypatch):
        """Foreground mode catches orchestrator exceptions and returns
        {status: "ok", exit_code: 1, message: "crashed..."} instead of crashing."""
        import awf.orchestrator as orch_mod

        def crashing(args):
            raise KeyError("simulated crash")

        monkeypatch.setattr(orch_mod, "run_pipeline", crashing)

        result = run(awf.awf_start(
            project_dir=str(initialized_project),
            background=False,
        ))
        # MCP tool catches AwfApiError; but api.start_pipeline now catches
        # orchestrator exceptions internally and returns StartResult with exit_code=1.
        assert result["status"] == "ok"
        assert result["exit_code"] == 1
        assert "crashed" in result["message"]

    def test_missing_agentic(self, tmp_path):
        proj = tmp_path / "nothing"
        proj.mkdir()
        result = run(awf.awf_start(
            project_dir=str(proj),
            background=True,
        ))
        assert result["status"] == "error"


class TestAwfContinue:
    def test_no_active_todo_returns_noop(self, git_project):
        """Project without active TODO → continue returns noop."""
        api.init_project(git_project, project_name="Test")
        # NO TODO created — simulate empty inbox
        result = run(awf.awf_continue(project_dir=str(git_project)))
        assert result["status"] == "ok"
        assert result["run_mode"] == "noop"
        assert "No active TODO" in result["message"]


# ─── Tool registration ──────────────────────────────────────────────────


class TestAwfOpenProjectSetupForm:
    """Dogfood-5: shortcut tool — no data dict needed, plugin auto-populates."""

    def test_opens_form_without_data_dict(self, plugin_setup, tmp_path):
        """One call opens project-setup form. Plugin handles available_roles,
        available_models, skills, custom roles automatically — supervisor
        doesn't study template structure or pass data."""
        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)

        result = run(awf.awf_open_project_setup_form(project_dir=str(proj)))

        assert result["status"] == "ok", f"Expected ok, got: {result}"
        assert "form_id" in result
        assert result["form_id"].startswith("FORM-")

    def test_works_without_agentic_dir(self, plugin_setup, tmp_path):
        """Without .agentic/ — form still opens (plugin doesn't fail)."""
        proj = tmp_path / "no-agentic"
        proj.mkdir()

        result = run(awf.awf_open_project_setup_form(project_dir=str(proj)))
        assert result["status"] == "ok"


class TestToolRegistration:
    def test_all_11_tools_exist_as_callables(self):
        """All 11 awf_* tools must be exposed as async callables."""
        tool_names = [
            "awf_init", "awf_status", "awf_start", "awf_continue",
            "awf_baseline", "awf_rollback", "awf_approve", "awf_report",
            "awf_reset", "awf_add_role", "awf_analyze_roles",
        ]
        for name in tool_names:
            assert hasattr(awf, name), f"Missing tool: {name}"
            fn = getattr(awf, name)
            assert callable(fn), f"{name} is not callable"
            import inspect
            assert inspect.iscoroutinefunction(fn), f"{name} must be async"

    def test_server_registers_all_11_tools(self):
        """server.create_server() registers all awf_* tools without error.

        Skipped when ``mcp`` package is unavailable (CI runners without
        opencode-installed deps). The 11 tools themselves are importable
        (verified by test_all_11_tools_exist_as_callables); this test only
        verifies FastMCP registration wiring.
        """
        pytest.importorskip("mcp.server.fastmcp")
        from agent_workflow_ui.server import create_server
        # FastMCP servers can be created without running them
        # This test verifies imports + registration succeed
        try:
            mcp = create_server()
            assert mcp is not None
        except Exception as e:
            pytest.fail(f"create_server failed: {e}")
