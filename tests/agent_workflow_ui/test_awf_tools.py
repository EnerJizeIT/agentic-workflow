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
from pathlib import Path

import pytest
from agent_workflow_ui.tools import awf

from awf import api

# AUD12-13 / test infra: import mcp at COLLECTION time, not inside a test.
# The autouse conftest fixture replaces subprocess.Popen with a function, and
# mcp evaluates a `subprocess.Popen[bytes]` annotation at class-creation —
# importing inside a test body therefore crashes on a patched Popen.
# (Same pattern as test_server_smoke.py.)
try:
    import mcp.server.fastmcp  # noqa: F401
    _MCP_AVAILABLE = True
except Exception:
    _MCP_AVAILABLE = False


@pytest.fixture
def git_project(tmp_git_repo):
    """Create a git-initialized empty project.

    AUD12-08: the git boilerplate is the shared tmp_git_repo fixture
    (tests/conftest.py).
    """
    return tmp_git_repo


@pytest.fixture
def mcp_project(git_project):
    """Project with .agentic/ already set up via api.init_project.

    AUD12-08: renamed from ``initialized_project`` — that name is taken by
    the root e2e fixture with a DIFFERENT contract (CLI ``awf init`` +
    pipeline.yaml). One name, one meaning.
    """
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
        assert result["project_name"] == "Repo"
        assert "supervisor_md" in result
        assert len(result["supervisor_md"]) > 0
        assert "plan_md" in result
        assert "vision_excerpt" in result
        assert result["pipeline_configured"] is False
        assert "awf_set_goal" in result["next_action"] or "цель" in result["next_action"]
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

    def test_already_initialized_cleans_runtime(self, git_project):
        """R1: awf init without force cleans runtime, preserves config."""
        run(awf.awf_init(project_dir=str(git_project)))
        result = run(awf.awf_init(project_dir=str(git_project)))
        assert result["status"] == "ok"
        # SMO: next_action is now phase-aware (not hardcoded "cleaned/preserved")
        assert "next_action" in result
        assert len(result["next_action"]) > 0

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

    def test_with_active_todo(self, mcp_project):
        inbox = mcp_project / ".agentic" / "inbox"
        outbox = mcp_project / ".agentic" / "outbox"
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task")
        result = run(awf.awf_status(project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert len(result["active_todos"]) == 1
        assert result["active_todos"][0]["todo_id"] == "TODO-0001"


# ─── awf_baseline ───────────────────────────────────────────────────────


class TestAwfBaseline:
    def test_creates_baseline(self, mcp_project):
        result = run(awf.awf_baseline(
            todo_id="TODO-0001",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "ok"
        assert result["todo_id"] == "TODO-0001"
        assert len(result["sha"]) == 40
        assert result["is_git_repo"] is True
        assert "BASELINE-TODO-0001.sha" in result["files_created"]
        assert (mcp_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").exists()

    def test_missing_agentic(self, tmp_path):
        result = run(awf.awf_baseline(
            todo_id="TODO-0001",
            project_dir=str(tmp_path),
        ))
        assert result["status"] == "error"


# ─── awf_rollback ───────────────────────────────────────────────────────


class TestAwfRollback:
    def test_dry_run(self, mcp_project):
        # First create baseline + change
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=mcp_project, text=True
        ).strip()
        (mcp_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n"
        )
        (mcp_project / "extra.txt").write_text("extra")
        subprocess.run(["git", "add", "-A"], cwd=mcp_project, check=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=mcp_project, check=True)

        result = run(awf.awf_rollback(
            todo_id="TODO-0001",
            project_dir=str(mcp_project),
            mode="dry-run",
        ))
        assert result["status"] == "ok"
        assert result["mode"] == "dry-run"
        assert result["ack_file"] is None
        # extra.txt still there
        assert (mcp_project / "extra.txt").exists()

    def test_hard_resets(self, mcp_project):
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=mcp_project, text=True
        ).strip()
        (mcp_project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n"
        )
        (mcp_project / "extra.txt").write_text("extra")
        subprocess.run(["git", "add", "-A"], cwd=mcp_project, check=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=mcp_project, check=True)

        result = run(awf.awf_rollback(
            todo_id="TODO-0001",
            project_dir=str(mcp_project),
            mode="hard",
        ))
        assert result["status"] == "ok"
        assert not (mcp_project / "extra.txt").exists()
        assert result["ack_file"] is not None

    def test_missing_baseline(self, mcp_project):
        result = run(awf.awf_rollback(
            todo_id="TODO-9999",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "error"
        assert "Baseline SHA not found" in result["error"]


# ─── awf_approve ────────────────────────────────────────────────────────


class TestAwfApprove:
    def test_creates_signal(self, mcp_project):
        result = run(awf.awf_approve(
            todo_id="TODO-0001",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "ok"
        assert "APPROVE-TODO-0001.ready" in result["signal_file"]
        assert (mcp_project / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").exists()

    def test_empty_todo_id(self, mcp_project):
        result = run(awf.awf_approve(
            todo_id="",
            project_dir=str(mcp_project),
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


# ─── awf_reject ─────────────────────────────────────────────────────────


class TestAwfReject:
    def test_reject_writes_review_and_does_not_kill(self, mcp_project, monkeypatch):
        """AUD08-04: the wrapper used to kill the waiting pipeline right
        after reject_commit while its docstring promised the engine handles
        REVIEW → replan itself. The kill is gone — the engine owns the
        REVIEW transition, so the state is deterministic for
        pipeline alive/dead × run/not-run."""
        kill_calls = {"n": 0}

        def spy_kill(*a, **kw):
            kill_calls["n"] += 1

        monkeypatch.setattr(awf.api, "kill_pipeline", spy_kill)

        result = run(awf.awf_reject(
            todo_id="TODO-0001",
            reason="diff has a bug",
            project_dir=str(mcp_project),
        ))

        assert result["status"] == "ok"
        review = mcp_project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md"
        assert review.is_file(), "REVIEW-{todo}.md must be written to the outbox"
        assert "diff has a bug" in review.read_text(encoding="utf-8")
        assert kill_calls["n"] == 0, (
            "awf_reject killed the pipeline — the docstring (engine handles "
            "REVIEW → replan) and the behavior must match"
        )
        assert "killed" not in result["next_action"].lower()

    def test_reject_requires_reason(self, mcp_project):
        result = run(awf.awf_reject(
            todo_id="TODO-0001",
            reason="   ",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "error"
        assert "reason" in result["error"]

    def test_reject_invalid_todo(self, mcp_project):
        result = run(awf.awf_reject(
            todo_id="NOT-A-TODO",
            reason="bad",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "error"
        assert "todo_id" in result["error"]


# ─── awf_dispatch_todo carry_over_from (RUN5 #1, TODO-0052) ──────────────


class TestDispatchCarryOverParam:
    """The MCP dispatch tool must expose carry_over_from and proxy it to the
    api (so a retry can pull the rejected attempt's files into its commit)."""

    def test_param_proxied_to_api(self, mcp_project, monkeypatch):
        captured = {}

        class _R:
            todo_id = "TODO-0002"
            baseline_sha = "0" * 40
            role_hint = None
            files_written = []
            pre_check_warnings = []
            carry_over_from = "TODO-0001"
            carry_over_files = ["src/a.py"]

            def as_dict(self):
                return {
                    "todo_id": self.todo_id,
                    "baseline_sha": self.baseline_sha,
                    "role_hint": self.role_hint,
                    "files_written": self.files_written,
                    "carry_over_from": self.carry_over_from,
                    "carry_over_files": self.carry_over_files,
                }

        def fake_dispatch(project_dir, content, *, role=None, todo_id=None,
                          pipeline=None, carry_over_from=None, include_untracked=None):
            captured["carry_over_from"] = carry_over_from
            return _R()

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        result = run(awf.awf_dispatch_todo(
            content="# retry",
            project_dir=str(mcp_project),
            carry_over_from="TODO-0001",
        ))
        assert result["status"] == "ok"
        assert captured["carry_over_from"] == "TODO-0001", (
            "carry_over_from must be proxied to api.dispatch_todo"
        )
        assert result["carry_over_files"] == ["src/a.py"]

    def test_absent_param_defaults_none(self, mcp_project, monkeypatch):
        captured = {}

        class _R:
            todo_id = "TODO-0002"
            baseline_sha = "0" * 40
            role_hint = None
            files_written = []
            pre_check_warnings = []
            carry_over_from = None
            carry_over_files = []

            def as_dict(self):
                return {
                    "todo_id": self.todo_id,
                    "baseline_sha": self.baseline_sha,
                    "role_hint": self.role_hint,
                    "files_written": self.files_written,
                    "carry_over_from": self.carry_over_from,
                    "carry_over_files": self.carry_over_files,
                }

        def fake_dispatch(project_dir, content, *, role=None, todo_id=None,
                          pipeline=None, carry_over_from=None, include_untracked=None):
            captured["carry_over_from"] = carry_over_from
            return _R()

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        run(awf.awf_dispatch_todo(content="# t", project_dir=str(mcp_project)))
        assert captured["carry_over_from"] is None

    def test_error_propagates(self, mcp_project, monkeypatch):
        def fake_dispatch(project_dir, content, *, role=None, todo_id=None,
                          pipeline=None, carry_over_from=None, include_untracked=None):
            raise api.AwfApiError("REJECT-TODO-0001.files not found")

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        result = run(awf.awf_dispatch_todo(
            content="# retry",
            project_dir=str(mcp_project),
            carry_over_from="TODO-0001",
        ))
        assert result["status"] == "error"
        assert "REJECT-TODO-0001" in result["error"]


# ─── awf_dispatch_todo include_untracked (RUN10 #4, TODO-0074) ───────────


class TestDispatchIncludeUntrackedParam:
    """The MCP dispatch tool must expose include_untracked, proxy it to the
    api, and carry the pre-existing-untracked warning in the answer."""

    def test_param_proxied_to_api(self, mcp_project, monkeypatch):
        captured = {}

        class _R:
            todo_id = "TODO-0042"
            baseline_sha = "0" * 40
            role_hint = None
            files_written = []
            pre_check_warnings = []
            pre_existing_untracked = ["y.md"]
            untracked_warning = "⚠️ 1 file(s) ... will NOT be included in the TODO-0042 commit: y.md."

            def as_dict(self):
                return {
                    "todo_id": self.todo_id,
                    "baseline_sha": self.baseline_sha,
                    "role_hint": self.role_hint,
                    "files_written": self.files_written,
                    "pre_existing_untracked": self.pre_existing_untracked,
                    "untracked_warning": self.untracked_warning,
                }

        def fake_dispatch(project_dir, content, *, role=None, todo_id=None,
                          pipeline=None, carry_over_from=None, include_untracked=None):
            captured["include_untracked"] = include_untracked
            return _R()

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        result = run(awf.awf_dispatch_todo(
            content="# t",
            project_dir=str(mcp_project),
            include_untracked=["x.md"],
        ))
        assert result["status"] == "ok"
        assert captured["include_untracked"] == ["x.md"], (
            "include_untracked must be proxied to api.dispatch_todo"
        )
        assert result["pre_existing_untracked"] == ["y.md"]
        assert "y.md" in result["next_action"], "the warning must reach the supervisor"

    def test_absent_param_defaults_none(self, mcp_project, monkeypatch):
        captured = {}

        class _R:
            todo_id = "TODO-0042"
            baseline_sha = "0" * 40
            role_hint = None
            files_written = []
            pre_check_warnings = []
            pre_existing_untracked = []
            untracked_warning = ""

            def as_dict(self):
                return {
                    "todo_id": self.todo_id,
                    "baseline_sha": self.baseline_sha,
                    "role_hint": self.role_hint,
                    "files_written": self.files_written,
                    "pre_existing_untracked": self.pre_existing_untracked,
                    "untracked_warning": self.untracked_warning,
                }

        def fake_dispatch(project_dir, content, *, role=None, todo_id=None,
                          pipeline=None, carry_over_from=None, include_untracked=None):
            captured["include_untracked"] = include_untracked
            return _R()

        monkeypatch.setattr(api, "dispatch_todo", fake_dispatch)
        run(awf.awf_dispatch_todo(content="# t", project_dir=str(mcp_project)))
        assert captured["include_untracked"] is None


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

    def test_with_done_task(self, mcp_project):
        ag = mcp_project / ".agentic"
        (ag / "inbox" / "TODO-0001.ready").touch()
        (ag / "outbox" / "DONE-TODO-0001.ready").touch()
        result = run(awf.awf_report(project_dir=str(mcp_project)))
        assert result["done_count"] == 1
        assert any(item["status"] == "OK" for item in result["items"])


# ─── awf_reset ──────────────────────────────────────────────────────────


class TestAwfReset:
    def test_default_cleans_runtime(self, mcp_project):
        ag = mcp_project / ".agentic"
        for d in ["inbox", "outbox", "context", "logs"]:
            (ag / d / "junk.txt").write_text("junk")
        result = run(awf.awf_reset(project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert "inbox" in result["cleaned_dirs"]
        assert "outbox" in result["cleaned_dirs"]

    def test_tasks_only(self, mcp_project):
        ag = mcp_project / ".agentic"
        (ag / "inbox" / "x.txt").write_text("x")
        (ag / "context" / "y.txt").write_text("y")
        result = run(awf.awf_reset(
            project_dir=str(mcp_project),
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
    def test_creates_role(self, mcp_project):
        result = run(awf.awf_add_role(
            name="qa",
            project_dir=str(mcp_project),
            description="QA engineer",
            model="glm-5.2",
        ))
        assert result["status"] == "ok"
        assert result["role_name"] == "qa"
        role_file = mcp_project / ".agentic" / "roles" / "qa.md"
        assert role_file.exists()
        assert "glm-5.2" in role_file.read_text()

    def test_empty_name(self, mcp_project):
        result = run(awf.awf_add_role(
            name="",
            project_dir=str(mcp_project),
        ))
        assert result["status"] == "error"

    def _make_global_skill(self, mcp_project, monkeypatch, name="demo-skill"):
        """Private XDG_CONFIG_HOME with one skill; returns its SKILL.md path."""
        monkeypatch.setenv(
            "XDG_CONFIG_HOME", str(mcp_project / "xdg-home")
        )
        skill_dir = mcp_project / "xdg-home" / "opencode" / "skills" / name
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\nname: demo-skill\n---\n\n# Demo Skill\n\nDemo body line.\n",
            encoding="utf-8",
        )
        return skill_file

    def test_from_skill_ok(self, mcp_project, monkeypatch):
        skill_file = self._make_global_skill(mcp_project, monkeypatch)
        result = run(awf.awf_add_role(
            name="demo-role",
            project_dir=str(mcp_project),
            from_skill="demo-skill",
        ))
        assert result["status"] == "ok", result
        role_file = mcp_project / ".agentic" / "roles" / "demo-role.md"
        assert role_file.is_file()
        content = role_file.read_text(encoding="utf-8")
        assert "Demo body line." in content
        assert str(skill_file) in content
        assert not content.startswith("---")

    def test_from_skill_name_defaults_to_skill(
        self, mcp_project, monkeypatch
    ):
        self._make_global_skill(mcp_project, monkeypatch)
        result = run(awf.awf_add_role(
            project_dir=str(mcp_project),
            from_skill="demo-skill",
        ))
        assert result["status"] == "ok", result
        assert (mcp_project / ".agentic" / "roles" / "demo-skill.md").is_file()

    def test_from_skill_unknown_lists_available(
        self, mcp_project, monkeypatch
    ):
        self._make_global_skill(mcp_project, monkeypatch)
        result = run(awf.awf_add_role(
            name="x",
            project_dir=str(mcp_project),
            from_skill="ghost-skill",
        ))
        assert result["status"] == "error"
        assert "ghost-skill" in result["error"]
        assert "demo-skill" in result["error"]

    def test_from_skill_occupied_refused_then_force(
        self, mcp_project, monkeypatch
    ):
        self._make_global_skill(mcp_project, monkeypatch)
        run(awf.awf_add_role(name="demo-skill", project_dir=str(mcp_project),
                             model="m"))
        result = run(awf.awf_add_role(
            name="demo-skill",
            project_dir=str(mcp_project),
            from_skill="demo-skill",
        ))
        assert result["status"] == "error"
        assert "already exists" in result["error"]
        result = run(awf.awf_add_role(
            name="demo-skill",
            project_dir=str(mcp_project),
            from_skill="demo-skill",
            force=True,
        ))
        assert result["status"] == "ok", result
        content = (mcp_project / ".agentic" / "roles" / "demo-skill.md"
                   ).read_text(encoding="utf-8")
        assert "Demo body line." in content


# ─── awf_analyze_roles ──────────────────────────────────────────────────


class TestAwfAnalyzeRoles:
    def test_dry_run_returns_result(self, mcp_project):
        # analyze requires team roles (not just supervisor.md)
        (mcp_project / ".agentic" / "roles" / "worker.md").write_text(
            "# ROLE: worker\nImplementation role\n"
        )
        result = run(awf.awf_analyze_roles(
            project_dir=str(mcp_project),
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
    def test_background_returns_pid(self, mcp_project):
        """Background mode returns immediately with a PID."""
        result = run(awf.awf_start(
            project_dir=str(mcp_project),
            background=True,
            auto=True,
        ))
        # Background launches subprocess; may succeed or fail to find pipeline
        # but should never block
        assert result["status"] in ("ok", "error")
        if result["status"] == "ok":
            assert result["run_mode"] == "background"
            assert result["run_id"] is not None or result["exit_code"] is not None

    def test_foreground_orchestrator_crash_returns_error_not_crash(self, mcp_project, monkeypatch):
        """Foreground mode catches orchestrator exceptions and returns
        {status: "ok", exit_code: 1, message: "crashed..."} instead of crashing."""
        import awf.orchestrator as orch_mod

        def crashing(args):
            raise KeyError("simulated crash")

        monkeypatch.setattr(orch_mod, "run_pipeline", crashing)

        result = run(awf.awf_start(
            project_dir=str(mcp_project),
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

    def test_todo_id_proxied_to_api(self, mcp_project, monkeypatch):
        """RUN8 #1 (TODO-0063): the MCP tool exposes todo_id (parity with
        awf_start) and proxies it to api.continue_pipeline."""
        captured = {}

        class _R:
            run_mode = "noop"
            run_id = None
            log_file = None
            exit_code = 0
            message = "No active TODO found."

            def as_dict(self):
                return {
                    "run_mode": self.run_mode,
                    "run_id": self.run_id,
                    "log_file": self.log_file,
                    "exit_code": self.exit_code,
                    "message": self.message,
                }

        def fake_continue(project_dir, **kwargs):
            captured.update(kwargs)
            return _R()

        monkeypatch.setattr(api, "continue_pipeline", fake_continue)
        result = run(awf.awf_continue(
            project_dir=str(mcp_project),
            todo_id="TODO-0007",
            ack="TODO-0007",
        ))
        assert result["status"] == "ok"
        assert captured["todo_id"] == "TODO-0007", (
            "todo_id must be proxied to api.continue_pipeline"
        )
        assert captured["ack"] == "TODO-0007"


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


class TestApiParity:
    """Every ``api.<name>`` call in the plugin must exist on ``awf.api``.

    Regression (dogfood-11): ``awf_retry_stage`` called ``api.retry_stage``
    which lived in ``awf.api.pipeline`` but was never re-exported from
    ``awf/api/__init__.py`` — the tool crashed with AttributeError at
    runtime. Existing tests missed it: the smoke test checked tool *names*
    only, and unit tests imported the function from the submodule directly.
    """

    def test_every_api_call_exists_on_awf_api(self):
        import inspect
        import re

        src = inspect.getsource(awf)
        names = sorted(set(re.findall(r"(?<![\w.])api\.(\w+)", src)))
        assert names, "no api.<name> calls found — regex or module source changed"
        missing = [n for n in names if not hasattr(api, n)]
        assert not missing, (
            f"tools/awf.py calls awf.api.{missing}, but awf/api/__init__.py "
            f"does not export it — add the name to the submodule import block "
            f"and to __all__"
        )


class TestToolRegistration:
    def test_core_tools_exist_as_callables(self):
        """The 11 core awf_* tools must be exposed as async callables.

        AUD08-08: renamed from "all 11 tools" — the full set is 32 (and
        grows); the strict count is pinned by test_all_tools_registered.
        """
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

    def test_server_registers_core_tools(self):
        """server.create_server() registers all awf_* tools without error.

        Skipped when ``mcp`` package is unavailable (CI runners without
        opencode-installed deps). The core tools themselves are importable
        (verified by test_core_tools_exist_as_callables); this test only
        verifies FastMCP registration wiring.
        """
        if not _MCP_AVAILABLE:
            pytest.skip("mcp package unavailable")
        from agent_workflow_ui.server import create_server
        # FastMCP servers can be created without running them
        # This test verifies imports + registration succeed
        try:
            mcp = create_server()
            assert mcp is not None
        except Exception as e:
            pytest.fail(f"create_server failed: {e}")


class TestWaitForEventClamp:
    """R3 (NEG-2026-09-19): the MCP transport cuts long calls — clamp them."""

    def test_timeout_clamped_and_flagged(self, monkeypatch):
        captured: dict = {}

        def fake_wait(project_dir, *, timeout, actionable_only=False):
            captured["timeout"] = timeout
            captured["actionable_only"] = actionable_only

            class _R:
                def as_dict(self):
                    return {"event_type": "timeout", "message": "no event",
                            "state_snapshot": {}, "suggested_timeout": 120}

            return _R()

        monkeypatch.setattr(api, "wait_for_event", fake_wait)
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(
            awf.awf_wait_for_event(project_dir="/tmp", timeout=9999, actionable_only=True)
        )

        assert captured["timeout"] == 600, "must clamp to the transport-safe cap"
        assert result["timeout_clamped"] is True
        assert result["timeout_requested"] == 9999

    def test_short_timeout_untouched(self, monkeypatch):
        captured: dict = {}

        def fake_wait(project_dir, *, timeout, actionable_only=False):
            captured["timeout"] = timeout

            class _R:
                def as_dict(self):
                    return {"event_type": "timeout", "message": "x",
                            "state_snapshot": {}, "suggested_timeout": 60}

            return _R()

        monkeypatch.setattr(api, "wait_for_event", fake_wait)
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=55))

        assert captured["timeout"] == 55
        assert "timeout_clamped" not in result

    @staticmethod
    def _fake_wait_with(event_type: str):
        def fake_wait(project_dir, *, timeout, actionable_only=False):
            class _R:
                def as_dict(self):
                    return {"event_type": event_type, "message": "x",
                            "state_snapshot": {}, "suggested_timeout": 120}

            return _R()

        return fake_wait

    def test_verify_event_gets_verify_next_action(self, monkeypatch):
        """AUD08-01 regression: a verify event must get the approve
        instruction, not the stuck 'timeout' one (dataclass vs dict bug)."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("verify"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "verify"
        assert "awf_approve" in result["next_action"]
        assert "Continue the run loop" not in result["next_action"]

    def test_blocked_event_gets_blocked_next_action(self, monkeypatch):
        """AUD08-01 regression: a blocked event must get the unblock
        instruction, not the stuck 'timeout' one."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("blocked"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "blocked"
        assert "Worker blocked" in result["next_action"]
        assert "Continue the run loop" not in result["next_action"]

    def test_done_event_gets_run_next_next_action(self, monkeypatch):
        """RUN6 #1: a done event inside a run must carry the exact next
        command — awf_run_next (the owner had to read git log instead)."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("done"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: True)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "done"
        assert "awf_run_next" in result["next_action"]
        assert "awf_dispatch_todo" not in result["next_action"]

    def test_timeout_in_active_run_says_continue_not_wait_for_user(self, monkeypatch):
        """RUN10 #1 (bug 2026-09-24): a timeout INSIDE an active run must
        offer the next loop call — never 'Wait for user' or 'DO NOT call
        again' (an agent following those literally stops the run)."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("timeout"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: True)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=30))

        assert "No event yet. Continue the run loop" in result["next_action"]
        assert "awf_wait_for_event(timeout=" in result["next_action"]
        assert "actionable_only=True" in result["next_action"]
        assert "Wait for user" not in result["next_action"]
        assert "DO NOT call" not in result["next_action"]

    def test_timeout_without_run_keeps_reactive_text(self, monkeypatch):
        """RUN10 #1 hard rule: outside a run the reactive wording is
        UNCHANGED."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("timeout"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=30))

        assert "DO NOT call awf_wait_for_event again. Wait for user." in result["next_action"]
        assert "Continue the run loop" not in result["next_action"]

    def test_done_event_gets_dispatch_next_action(self, monkeypatch):
        """RUN6 #1: a done event outside a run (single start) must lead to
        awf_dispatch_todo, not awf_run_next."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("done"))
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: False)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "done"
        assert "awf_dispatch_todo" in result["next_action"]
        assert "awf_run_next" not in result["next_action"]


class TestWaitForEventCapNote:
    """B3 (run2 report): every next_action carries the ACTUAL single-wait
    cap — the MCP client transport cuts a wait at ~55s by default, so the
    supervisor stops hammering 180s waits that die at ~55s (-32001)."""

    @staticmethod
    def _run(event_type: str, timeout, monkeypatch, run_active: bool = False):
        def fake_wait(project_dir, *, timeout, actionable_only=False):
            class _R:
                def as_dict(self):
                    return {"event_type": event_type, "message": "x",
                            "state_snapshot": {}, "suggested_timeout": 45}

            return _R()

        monkeypatch.setattr(api, "wait_for_event", fake_wait)
        monkeypatch.setattr(
            api, "run_is_active", lambda *a, **kw: run_active
        )
        return asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=timeout))

    def test_next_action_default_cap_names_tool_setting(self, monkeypatch):
        """RUN10 #2: default cap — the note names the EXACT setting with
        the CONCRETE value (mcp timeout 600000 ms → wait.cap_seconds: 570)
        and never claims it is the transport cap."""
        import awf.api.wait_event as wait_event_mod

        monkeypatch.setattr(
            wait_event_mod, "mcp_transport_timeout_ms", lambda *a, **k: 600000
        )
        result = self._run("timeout", 30, monkeypatch)

        cap = api.TRANSPORT_CAP
        assert f"<= {cap}s" in result["next_action"]
        assert "not the transport" in result["next_action"]
        assert "wait.cap_seconds: 570" in result["next_action"]
        assert "AWF_WAIT_CAP=570" in result["next_action"]
        assert "MCP client transport cap" not in result["next_action"]
        assert "raise the mcp timeout" not in result["next_action"]

    def test_next_action_default_cap_unknown_transport(self, monkeypatch):
        """RUN10 #2: opencode.json unreadable — no number, still the tool
        setting; the mcp-timeout clause is allowed back (unknown)."""
        import awf.api.wait_event as wait_event_mod

        monkeypatch.setattr(
            wait_event_mod, "mcp_transport_timeout_ms", lambda *a, **k: None
        )
        result = self._run("timeout", 30, monkeypatch)

        assert f"<= {api.TRANSPORT_CAP}s" in result["next_action"]
        assert "not the transport" in result["next_action"]
        assert "wait.cap_seconds" in result["next_action"]
        assert "wait.cap_seconds: " not in result["next_action"]
        assert "raise the mcp timeout in opencode.json" in result["next_action"]

    def test_next_action_cap_present_for_every_event(self, monkeypatch):
        """The note is on EVERY response, not just timeout/stage_changed."""
        for event_type in ("verify", "blocked", "salvage", "checkpoint", "done", "idle"):
            result = self._run(event_type, 30, monkeypatch)
            assert f"<= {api.TRANSPORT_CAP}s" in result["next_action"], event_type

    def test_run_mode_next_action_carries_cap_too(self, monkeypatch):
        result = self._run("timeout", 30, monkeypatch, run_active=True)

        assert "Continue the run loop" in result["next_action"]
        assert "Wait for user" not in result["next_action"]
        assert f"<= {api.TRANSPORT_CAP}s" in result["next_action"]

    def test_clamped_next_action_says_so_explicitly(self, monkeypatch):
        import awf.api.wait_event as wait_event_mod

        # unknown transport → deterministic note with the tool setting
        monkeypatch.setattr(
            wait_event_mod, "mcp_transport_timeout_ms", lambda *a, **k: None
        )
        result = self._run("timeout", 9999, monkeypatch)

        assert result["timeout_clamped"] is True
        assert "clamped to 600" in result["next_action"]
        assert f"<= {api.TRANSPORT_CAP}s" in result["next_action"]
        assert "wait.cap_seconds" in result["next_action"]

    def test_suggested_fallback_respects_cap(self, monkeypatch):
        """A result without suggested_timeout must not push a 180s wait."""
        def fake_wait(project_dir, *, timeout, actionable_only=False):
            class _R:
                def as_dict(self):
                    return {"event_type": "timeout", "message": "x",
                            "state_snapshot": {}}

            return _R()

        monkeypatch.setattr(api, "wait_for_event", fake_wait)
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: True)

        result = asyncio.run(
            awf.awf_wait_for_event(project_dir="/tmp", timeout=10, actionable_only=True)
        )

        assert f"timeout={api.TRANSPORT_CAP}" in result["next_action"]

    def test_next_action_raised_cap_drops_mcp_advice(self, monkeypatch, tmp_path):
        """RUN6 #3: with wait.cap_seconds raised in the project config the
        note names the raised cap and the stale 'raise the mcp timeout in
        opencode.json' advice disappears — the owner already raised the
        ceiling themselves, so the default-transport hint under-sells it."""
        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        ag = tmp_path / ".agentic"
        ag.mkdir()
        (ag / "config.yaml").write_text("wait:\n  cap_seconds: 300\n", encoding="utf-8")

        def fake_wait(project_dir, *, timeout, actionable_only=False):
            class _R:
                def as_dict(self):
                    return {"event_type": "timeout", "message": "x",
                            "state_snapshot": {}, "suggested_timeout": 240}

            return _R()

        monkeypatch.setattr(api, "wait_for_event", fake_wait)
        monkeypatch.setattr(api, "run_is_active", lambda *a, **kw: True)

        result = asyncio.run(
            awf.awf_wait_for_event(project_dir=str(tmp_path), timeout=10)
        )

        assert "<= 300s" in result["next_action"]
        assert "project wait cap" in result["next_action"]
        assert "opencode.json" not in result["next_action"]
        assert "raise the mcp timeout" not in result["next_action"]


# ─── FU-19 (TODO-0023): Part B — AUD-08 wrapper parity ──────────────────


class TestAwfStartTodoId:
    """AUD08-02: awf_start gained todo_id (parity with api.start_pipeline / CLI --todo)."""

    def test_todo_id_is_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_start(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "start_pipeline", spy_start)

        result = run(awf.awf_start(project_dir="/tmp", todo_id="TODO-0015"))

        assert result["status"] == "error"  # spy aborted the call
        assert calls and calls[0]["todo_id"] == "TODO-0015"

    def test_todo_id_defaults_empty(self, monkeypatch):
        calls: list[dict] = []

        def spy_start(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "start_pipeline", spy_start)

        run(awf.awf_start(project_dir="/tmp"))

        assert calls and calls[0]["todo_id"] == ""


class TestAwfContinueBackground:
    """AUD08-02: awf_continue gained background (API default True — MCP parity)."""

    def test_background_default_true(self, monkeypatch):
        calls: list[dict] = []

        def spy_continue(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "continue_pipeline", spy_continue)

        run(awf.awf_continue(project_dir="/tmp"))

        assert calls and calls[0]["background"] is True

    def test_background_false_is_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_continue(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "continue_pipeline", spy_continue)

        run(awf.awf_continue(project_dir="/tmp", background=False))

        assert calls and calls[0]["background"] is False


class TestAwfRunNextErrorWrapping:
    """AUD08-07: run_next went through _exec — no exception may escape the tool."""

    def test_api_error_is_wrapped(self, monkeypatch):
        def spy_run_next(**kw):
            raise api.AwfApiError("no active run")

        monkeypatch.setattr(api, "run_next", spy_run_next)

        result = run(awf.awf_run_next(project_dir="/tmp"))

        assert result["status"] == "error"
        assert "no active run" in result["error"]

    def test_unexpected_error_is_wrapped(self, monkeypatch):
        def spy_run_next(**kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(api, "run_next", spy_run_next)

        result = run(awf.awf_run_next(project_dir="/tmp"))

        assert result["status"] == "error"
        assert "RuntimeError" in result["error"]

    def test_refused_action_is_ok_without_dashboard(self, monkeypatch):
        """A refused launch is a normal outcome (status ok) — and must NOT
        trigger the dashboard branch (that is for action == started)."""
        from awf.api import RunNextResult

        def spy_run_next(**kw):
            return RunNextResult(
                action="refused", todo_id="TODO-0002", message="previous not finished"
            )

        monkeypatch.setattr(api, "run_next", spy_run_next)

        result = run(awf.awf_run_next(project_dir="/tmp"))

        assert result["status"] == "ok"
        assert result["action"] == "refused"
        assert "dashboard_opened" not in result


class TestAwfWaitForEventTimeout:
    """AUD08-07: a non-numeric timeout must degrade to an error dict."""

    def test_non_numeric_timeout_returns_error(self):
        result = run(awf.awf_wait_for_event(project_dir="/tmp", timeout="abc"))

        assert result["status"] == "error"
        assert "timeout must be an integer" in result["error"]


class TestIncrementPlanningBoundaries:
    """AUD08-10: validation now matches the docstring — exactly 2..5 variants."""

    @staticmethod
    def _variants(n):
        return [{"id": chr(ord("A") + i), "title": f"V{i}"} for i in range(n)]

    def test_single_variant_rejected(self, plugin_setup, tmp_path):
        result = run(
            awf.awf_open_increment_planning_form(
                variants=self._variants(1), project_dir=str(tmp_path)
            )
        )
        assert result["status"] == "error"
        assert "at least 2" in result["error"]

    def test_two_variants_ok(self, plugin_setup, tmp_path):
        result = run(
            awf.awf_open_increment_planning_form(
                variants=self._variants(2), project_dir=str(tmp_path)
            )
        )
        assert result["status"] == "ok", result
        assert result["form_id"].startswith("FORM-")

    def test_five_variants_ok(self, plugin_setup, tmp_path):
        result = run(
            awf.awf_open_increment_planning_form(
                variants=self._variants(5), project_dir=str(tmp_path)
            )
        )
        assert result["status"] == "ok", result

    def test_six_variants_rejected(self, plugin_setup, tmp_path):
        result = run(
            awf.awf_open_increment_planning_form(
                variants=self._variants(6), project_dir=str(tmp_path)
            )
        )
        assert result["status"] == "error"
        assert "too many" in result["error"]


class TestDashboardErrorField:
    """AUD08-11: every status:"error" from a tool carries an error message."""

    def test_dashboard_failure_has_error_message(self, tmp_path, monkeypatch):
        # No live server port (absent) + generate_dashboard produces no
        # current.html (patched to a no-op) → the "cannot open" branch.
        import awf.api.dashboard as dash_mod

        monkeypatch.setattr(dash_mod, "generate_dashboard", lambda pd: None)

        result = run(awf.awf_open_pipeline_dashboard(project_dir=str(tmp_path)))

        assert result["status"] == "error"
        assert result["opened"] is False
        assert result.get("error"), "error dict without an error message"
        assert "dashboard" in result["error"].lower()


class TestRejectSingleValidationLayer:
    """AUD08-13: validation lives in api.reject_commit; the tool just wraps it."""

    def test_invalid_todo_id_rejected_via_api(self, git_project):
        result = run(
            awf.awf_reject("garbage", "reason", project_dir=str(git_project))
        )
        assert result["status"] == "error"
        assert "TODO-NNNN" in result["error"]

    def test_empty_reason_rejected_via_api(self, git_project):
        result = run(
            awf.awf_reject("TODO-0001", "   ", project_dir=str(git_project))
        )
        assert result["status"] == "error"
        assert "reason" in result["error"].lower()

    def test_valid_reject_writes_review(self, git_project):
        api.init_project(git_project, project_name="RejectTest")
        result = run(
            awf.awf_reject("TODO-0001", "fix the edge case", project_dir=str(git_project))
        )
        assert result["status"] == "ok", result
        assert (
            git_project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md"
        ).is_file()


class TestRunStartStopFlags:
    """AUD08-16: stop_flags_json that is valid JSON but not a map must fail loudly."""

    def test_json_list_rejected(self, git_project):
        result = run(
            awf.awf_run_start(
                project_dir=str(git_project),
                stop_flags_json='["TODO-0012"]',
            )
        )
        assert result["status"] == "error"
        assert "JSON object" in result["error"]
        assert "list" in result["error"]

    def test_invalid_json_rejected(self, git_project):
        result = run(
            awf.awf_run_start(
                project_dir=str(git_project),
                stop_flags_json="{not json",
            )
        )
        assert result["status"] == "error"
        assert "not valid JSON" in result["error"]

    def test_valid_map_is_proxied(self, git_project, monkeypatch):
        calls: list[dict] = []

        def spy_run_start(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "run_start", spy_run_start)

        run(
            awf.awf_run_start(
                project_dir=str(git_project),
                stop_flags_json='{"TODO-0012": ["phase-boundary"]}',
            )
        )
        assert calls
        assert calls[0]["stop_flags"] == {"TODO-0012": ["phase-boundary"]}


# ─── Dashboard opening (AUD08-05 / AUD10-05) ───────────────────────────


class TestDashboardOpen:
    """One shared implementation, no event-loop blocking, liveness-checked
    port (a dead port file must fall back to file://, not open a dead URL)."""

    def _dead_port(self) -> int:
        import socket

        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()  # nothing listens anymore — the port is dead
        return port

    def _write_port_file(self, project: Path, port: int) -> None:
        state_dir = project / ".agentic" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "dashboard_port").write_text(str(port), encoding="utf-8")

    def test_open_dashboard_falls_back_when_port_dead(self, mcp_project, monkeypatch):
        self._write_port_file(mcp_project, self._dead_port())
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        result = run(awf.awf_open_pipeline_dashboard(project_dir=str(mcp_project)))

        assert result["status"] == "ok"
        assert result["method"] == "file"
        assert opened and opened[0].startswith("file://")

    def test_opens_live_http_when_port_alive(self, mcp_project, monkeypatch):
        from awf.api.dashboard_server import start_dashboard_server

        port, server = start_dashboard_server(mcp_project)
        try:
            self._write_port_file(mcp_project, port)
            opened: list[str] = []
            monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

            result = awf._open_dashboard_sync(mcp_project, wait=False)

            assert result["method"] == "http"
            assert result["url"] == f"http://127.0.0.1:{port}"
            assert opened == [result["url"]]
        finally:
            server.shutdown()
            server.server_close()

    def test_no_port_file_and_no_dashboard(self, git_project, monkeypatch):
        # Fallback cannot produce a page: generate_dashboard writes nothing.
        monkeypatch.setattr(
            "awf.api.dashboard.generate_dashboard", lambda project_dir: None
        )
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        result = awf._open_dashboard_sync(git_project, wait=False)

        assert result["opened"] is False
        assert result["method"] == "none"
        assert opened == []


class TestNoLoopBlockingSleep:
    """AUD08-05: time.sleep inside an async body froze the whole MCP event
    loop (up to 5 s per background start) — it must live only in the sync
    helper that callers run through asyncio.to_thread."""

    def test_no_sleep_inside_async_functions(self):
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(awf))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "sleep"
                ):
                    offenders.append(f"{node.name}:{sub.lineno}")
        assert not offenders, f"time.sleep inside async body: {offenders}"

    def test_all_dashboard_opens_use_shared_helper(self):
        """Every dashboard open goes through one helper: five
        to_thread(_open_dashboard_sync, ...) call sites — the
        start/continue/retry/run_next tools + the _open_dashboard_browser
        facade (awf_open_pipeline_dashboard). No inline copies."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(awf))
        count = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "to_thread"
                and node.args
                and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "_open_dashboard_sync"
            ):
                count += 1
        assert count == 5  # 4 tools + the _open_dashboard_browser facade


# ─── FU-18 (TODO-0032): prompt/docstring consistency ────────────────────


class TestAnalyzeRolesNextActionPhase:
    """AUD08-06: next_action was unconditional ('call awf_confirm_normalized')
    — from a 'run' phase that steered a weak model into a phase jump.
    The hint must follow the ACTUAL phase."""

    def _prep(self, mcp_project, phase):
        from awf.pipeline_state import write_state

        write_state(mcp_project, phase=phase)
        (mcp_project / ".agentic" / "roles" / "worker.md").write_text(
            "# ROLE: worker\nImplementation role\n"
        )

    def test_run_phase_does_not_steer_to_confirm_normalized(self, mcp_project):
        self._prep(mcp_project, "run")
        result = run(awf.awf_analyze_roles(project_dir=str(mcp_project), dry_run=True))
        assert result["status"] == "ok"
        # no imperative steering — the hint must NOT tell the model to call
        # it (mentioning it as forbidden is fine)
        assert "Call awf_confirm_normalized" not in result["next_action"]
        assert "do NOT call awf_confirm_normalized" in result["next_action"]

    def test_normalize_phase_keeps_confirm_normalized(self, mcp_project):
        self._prep(mcp_project, "normalize")
        result = run(awf.awf_analyze_roles(project_dir=str(mcp_project), dry_run=True))
        assert result["status"] == "ok"
        assert "confirm_normalized" in result["next_action"]


class TestApproveNextActionFacts:
    """AUD05-03: the fixed 'approved and committed. Pipeline exited.'
    promised a commit and an exit that approve does not cause.
    next_action must be built from run/pipeline facts."""

    T = "TODO-0001"

    def test_active_run_continues_run_loop(self, mcp_project):
        """RUN10 #1 (bug 2026-09-24): approve inside a run leads to
        awf_run_next — never 'Wait for the user before the next TODO'."""
        from awf import run_state

        run_state.write_run(
            mcp_project, active=True, queue=[self.T], started_at=run_state.now_iso(),
        )
        result = run(awf.awf_approve(
            self.T, project_dir=str(mcp_project),
            evidence="pytest -q → 348 passed; verdict: approve",
        ))
        assert result["status"] == "ok"
        assert "Continue the run loop: awf_run_next" in result["next_action"]
        assert "Wait for the user" not in result["next_action"]
        assert "committed" not in result["next_action"]
        assert "exited" not in result["next_action"].lower()

    def test_dead_pipeline_no_false_promise(self, mcp_project):
        result = run(awf.awf_approve(self.T, project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert "committed" not in result["next_action"]
        assert "exited" not in result["next_action"].lower()
        assert "not running" in result["next_action"]

    def test_live_pipeline_signal_waited_in_inbox(self, mcp_project, monkeypatch):
        class _St:
            pipeline_running = True

        monkeypatch.setattr(api, "get_status", lambda *a, **kw: _St())
        result = run(awf.awf_approve(self.T, project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert "committed" not in result["next_action"]
        assert "not running" not in result["next_action"]


class TestStartNextActionDashboard:
    """AUD08-15: next_action claimed 'Dashboard already opened' even when
    the open failed (dashboard_opened=False) — it must agree with the fact."""

    def _fake_start(self, monkeypatch):
        class _R:
            def as_dict(self):
                return {"run_mode": "background", "run_id": 12345,
                        "log_file": "x.log", "message": "started"}

        monkeypatch.setattr(api, "start_pipeline", lambda *a, **kw: _R())

    def test_failed_dashboard_not_claimed_opened(self, mcp_project, monkeypatch):
        self._fake_start(monkeypatch)
        monkeypatch.setattr(
            awf, "_open_dashboard_sync",
            lambda pd, wait=False: {"opened": False, "method": "none"},
        )
        result = run(awf.awf_start(project_dir=str(mcp_project), background=True))
        assert result["status"] == "ok"
        assert result["dashboard_opened"] is False
        assert "already opened" not in result["next_action"]
        assert "awf_open_pipeline_dashboard" in result["next_action"]

    def test_opened_dashboard_keeps_idle_instruction(self, mcp_project, monkeypatch):
        self._fake_start(monkeypatch)
        monkeypatch.setattr(
            awf, "_open_dashboard_sync",
            lambda pd, wait=False: {"opened": True, "method": "http", "url": "http://x"},
        )
        result = run(awf.awf_start(project_dir=str(mcp_project), background=True))
        assert result["status"] == "ok"
        assert result["dashboard_opened"] is True
        assert "GO IDLE" in result["next_action"]


class TestAwfMetricsParams:
    """U8c: awf_metrics gained refresh_subscriptions + mirror (MCP parity
    with `awf metrics --refresh-subscriptions` / `--no-mirror`)."""

    def test_defaults_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_metrics(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "collect_metrics", spy_metrics)

        result = run(awf.awf_metrics(project_dir="/tmp"))

        assert result["status"] == "error"  # spy aborted the call
        assert calls and calls[0]["refresh_subscriptions"] is False
        assert calls[0]["mirror"] is True
        assert calls[0]["all_projects"] is False  # RUN10 #3: default scope

    def test_params_are_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_metrics(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "collect_metrics", spy_metrics)

        result = run(
            awf.awf_metrics(
                project_dir="/tmp",
                refresh_subscriptions=True,
                mirror=False,
                all_projects=True,  # RUN10 #3
            )
        )

        assert result["status"] == "error"
        assert calls and calls[0]["refresh_subscriptions"] is True
        assert calls[0]["mirror"] is False
        assert calls[0]["all_projects"] is True


class TestAwfFeedback:
    """RUN4 #2: awf_feedback — MCP parity with `awf feedback` (CLI)."""

    def test_params_are_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_feedback(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "feedback", spy_feedback)

        result = run(
            awf.awf_feedback(
                project_dir="/tmp",
                type="bug",
                title="Pipeline hangs",
                body="что делал",
                severity="high",
                stdout=True,
            )
        )

        assert result["status"] == "error"  # spy aborted the call
        assert calls, "api.feedback was not called"
        assert calls[0]["ftype"] == "bug"
        assert calls[0]["title"] == "Pipeline hangs"
        assert calls[0]["body"] == "что делал"
        assert calls[0]["severity"] == "high"
        assert calls[0]["stdout"] is True

    def test_defaults_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_feedback(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "feedback", spy_feedback)

        run(awf.awf_feedback(project_dir="/tmp", type="feature", title="t"))

        assert calls[0]["ftype"] == "feature"
        assert calls[0]["title"] == "t"
        assert calls[0]["body"] == ""
        assert calls[0]["severity"] == ""
        assert calls[0]["stdout"] is False

    def test_bad_type_returns_error_dict(self, git_project):
        """A validation refusal comes back as {status: error, error}, not a raise."""
        result = run(
            awf.awf_feedback(project_dir=str(git_project), type="hotfix", title="x")
        )
        assert result["status"] == "error"
        assert "type" in result["error"]


class TestAwfTodoRetire:
    """RUN5 #2: awf_todo_retire — MCP parity with `awf todo-retire` (CLI)."""

    def _ghost(self, project: Path) -> None:
        """mcp_project ships TODO-0001 (md + .ready); add the reject tail."""
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "DONE-TODO-0001.md").write_text("worker claim")
        (outbox / "REVIEW-TODO-0001.md").write_text("rejected")

    def test_params_are_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_retire(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "retire_todo", spy_retire)

        result = run(
            awf.awf_todo_retire(
                todo_id="TODO-0035",
                reason="rejected at verify",
                project_dir="/tmp",
            )
        )

        assert result["status"] == "error"  # spy aborted the call
        assert calls, "api.retire_todo was not called"
        assert calls[0]["todo_id"] == "TODO-0035"
        assert calls[0]["reason"] == "rejected at verify"

    def test_empty_reason_returns_error_dict(self, mcp_project):
        self._ghost(mcp_project)

        result = run(
            awf.awf_todo_retire(todo_id="TODO-0001", project_dir=str(mcp_project))
        )

        assert result["status"] == "error"
        assert "reason" in result["error"]
        assert (mcp_project / ".agentic" / "inbox" / "TODO-0001.md").is_file()

    def test_retire_ghost_ok(self, mcp_project):
        self._ghost(mcp_project)

        result = run(
            awf.awf_todo_retire(
                todo_id="TODO-0001",
                reason="rejected at verify",
                project_dir=str(mcp_project),
            )
        )

        assert result["status"] == "ok"
        inbox = mcp_project / ".agentic" / "inbox"
        assert not (inbox / "TODO-0001.md").exists()
        assert not (inbox / "TODO-0001.ready").exists()
        done_dir = mcp_project / ".agentic" / "done" / "TODO-0001"
        assert (done_dir / "TODO.md").is_file()
        assert list(done_dir.glob("RETIRED-*.md"))
        assert (
            [t["todo_id"] for t in api.get_status(mcp_project).active_todos]
            == []
        )


class TestCurrentStepLiveProject:
    """RUN3-7 (TODO-0049): awf_current_step distinguishes a NEW project
    (setup chain, phase 'goal') from a LIVE one (configured + completed
    work → working phase 'run'), without changing the response shape."""

    def _make_live(self, project: Path) -> None:
        """Pipeline with a worker stage + one archived TODO."""
        pipes = project / ".agentic" / "pipelines"
        pipes.mkdir(parents=True, exist_ok=True)
        (pipes / "default.yaml").write_text(
            "name: default\nstages:\n"
            "  - name: plan\n    role: supervisor\n    kind: plan\n"
            "  - name: worker\n    role: worker\n    kind: execute\n"
            "  - name: verify\n    role: supervisor\n    kind: verify\n",
            encoding="utf-8",
        )
        done = project / ".agentic" / "done" / "TODO-0001"
        done.mkdir(parents=True, exist_ok=True)
        (done / "TODO.md").write_text("# Task\n", encoding="utf-8")

    def test_live_project_no_goal_returns_run(self, mcp_project):
        self._make_live(mcp_project)
        result = run(awf.awf_current_step(project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert result["phase"] == "run"
        assert result["goal"] is None
        # Response shape unchanged: exactly these keys.
        assert set(result) == {"status", "phase", "prompt", "goal"}
        # The one explanatory line for a live project without a stored goal.
        assert "setup chain" in result["prompt"]

    def test_new_project_no_goal_returns_goal(self, mcp_project):
        """No pipeline, empty done/ → the setup chain as before. The
        init-time config (models: {supervisor: stub}) must NOT count as
        'configured with work' on its own."""
        result = run(awf.awf_current_step(project_dir=str(mcp_project)))
        assert result["status"] == "ok"
        assert result["phase"] == "goal"
        assert result["goal"] is None
        assert set(result) == {"status", "phase", "prompt", "goal"}
        assert "setup chain" not in result["prompt"]


class TestAwfTodoUpdate:
    """RUN6 #4: awf_todo_update — MCP parity with `awf todo-update` (CLI)."""

    def test_params_are_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_update(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "update_todo", spy_update)

        result = run(
            awf.awf_todo_update(
                todo_id="TODO-0001",
                content="new text",
                project_dir="/tmp",
                reason="scope cut",
            )
        )

        assert result["status"] == "error"  # spy aborted the call
        assert calls, "api.update_todo was not called"
        assert calls[0]["todo_id"] == "TODO-0001"
        assert calls[0]["content"] == "new text"
        assert calls[0]["reason"] == "scope cut"

    def test_update_keeps_number_and_ready(self, mcp_project):
        result = run(
            awf.awf_todo_update(
                todo_id="TODO-0001",
                content="# Task v2",
                project_dir=str(mcp_project),
            )
        )

        assert result["status"] == "ok"
        assert result["todo_id"] == "TODO-0001"
        inbox = mcp_project / ".agentic" / "inbox"
        assert (inbox / "TODO-0001.md").read_text() == "# Task v2"
        assert (inbox / "TODO-0001.ready").is_file()
        backup = mcp_project / result["backup"]
        assert backup.is_file()
        assert backup.read_text() == "# Task"

    def test_started_todo_returns_error_dict(self, mcp_project):
        outbox = mcp_project / ".agentic" / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "PROGRESS-TODO-0001.md").write_text("half done")

        result = run(
            awf.awf_todo_update(
                todo_id="TODO-0001",
                content="v2",
                project_dir=str(mcp_project),
            )
        )

        assert result["status"] == "error"
        assert "in flight" in result["error"]
        assert (
            mcp_project / ".agentic" / "inbox" / "TODO-0001.md"
        ).read_text() == "# Task"

    def test_missing_todo_returns_error_dict(self, mcp_project):
        result = run(
            awf.awf_todo_update(
                todo_id="TODO-0099",
                content="v2",
                project_dir=str(mcp_project),
            )
        )

        assert result["status"] == "error"
        assert "not found" in result["error"]

    def test_empty_content_returns_error_dict(self, mcp_project):
        result = run(
            awf.awf_todo_update(
                todo_id="TODO-0001",
                content="   ",
                project_dir=str(mcp_project),
            )
        )

        assert result["status"] == "error"
        assert "content is empty" in result["error"]


class TestAwfTreeSha:
    """RUN6 #4: awf_tree_sha — MCP parity with `awf tree-sha` (CLI)."""

    def test_returns_git_utils_fingerprint(self, git_project):
        from awf import git_utils

        result = run(awf.awf_tree_sha(project_dir=str(git_project)))

        assert result["status"] == "ok"
        assert result["sha"] == git_utils.tree_fingerprint(git_project)
        assert len(result["sha"]) == 64

    def test_stable_and_changes_after_edit(self, git_project):
        first = run(awf.awf_tree_sha(project_dir=str(git_project)))
        second = run(awf.awf_tree_sha(project_dir=str(git_project)))
        assert first["sha"] == second["sha"]

        (git_project / "README.md").write_text("tampered\n")
        third = run(awf.awf_tree_sha(project_dir=str(git_project)))
        assert third["sha"] != first["sha"]

    def test_non_repo_returns_error_dict(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()

        result = run(awf.awf_tree_sha(project_dir=str(plain)))

        assert result["status"] == "error"
        assert "git repo" in result["error"]

    def test_repo_without_commits_returns_error_dict(self, tmp_path):
        repo = tmp_path / "nocommits"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

        result = run(awf.awf_tree_sha(project_dir=str(repo)))

        assert result["status"] == "error"
        assert "fingerprint" in result["error"]


# ─── RUN6 #5 (TODO-0060): next_action smoke ──────────────────────────────


class TestNextActionSmoke:
    """Every answer leads to the next step.

    Key tools (status, brief, start, run_next, approve, reject, unblock,
    todo_remove, todo_retire, wait_for_event) carry a non-empty
    ``next_action`` in their response.
    """

    def test_status(self, mcp_project):
        r = run(awf.awf_status(project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert isinstance(r.get("next_action"), str) and r["next_action"]

    def test_brief(self, mcp_project):
        r = run(awf.awf_brief(project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert r.get("next_action")

    def test_start_background(self, mcp_project, monkeypatch):
        class _FakeStart:
            def as_dict(self):
                return {
                    "run_mode": "background",
                    "run_id": 4242,
                    "log_file": "x.log",
                    "message": "started",
                }

        monkeypatch.setattr(
            awf.api, "start_pipeline", lambda **kw: _FakeStart()
        )
        monkeypatch.setattr(
            awf,
            "_open_dashboard_sync",
            lambda *a, **k: {"opened": True, "method": "http", "url": "http://x"},
        )
        r = run(awf.awf_start(project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert "GO IDLE" in r["next_action"]

    def test_run_next(self, mcp_project, monkeypatch):
        class _FakeRunNext:
            def as_dict(self):
                return {
                    "action": "started",
                    "todo_id": "TODO-0001",
                    "run_mode": "background",
                    "run_id": 1,
                    "log_file": "x.log",
                    "next_action": "GO IDLE — the run loop continues on `done`.",
                }

        monkeypatch.setattr(awf.api, "run_next", lambda **kw: _FakeRunNext())
        monkeypatch.setattr(
            awf,
            "_open_dashboard_sync",
            lambda *a, **k: {"opened": True, "method": "http", "url": "http://x"},
        )
        r = run(awf.awf_run_next(project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert r.get("next_action")

    def test_approve(self, mcp_project):
        r = run(awf.awf_approve("TODO-0001", project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert r.get("next_action")

    def test_reject(self, mcp_project):
        r = run(awf.awf_reject("TODO-0001", "bad work", project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert r.get("next_action")

    def test_unblock(self, mcp_project):
        outbox = mcp_project / ".agentic" / "outbox"
        outbox.mkdir(exist_ok=True)
        (outbox / "BLOCKED-TODO-0001.ready").touch()
        r = run(awf.awf_unblock("TODO-0001", project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert "awf_start" in r["next_action"]

    def test_todo_remove(self, mcp_project):
        inbox = mcp_project / ".agentic" / "inbox"
        (inbox / "TODO-0002.md").write_text("# TODO-0002\n", encoding="utf-8")
        r = run(awf.awf_todo_remove("TODO-0002", project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert "awf_dispatch_todo" in r["next_action"]

    def test_todo_retire(self, mcp_project):
        r = run(
            awf.awf_todo_retire(
                "TODO-0001", reason="obsolete", project_dir=str(mcp_project)
            )
        )
        assert r["status"] == "ok"
        assert "RETIRED" in r["next_action"]

    def test_wait_for_event(self, mcp_project):
        r = run(awf.awf_wait_for_event(project_dir=str(mcp_project), timeout=1))
        assert r["status"] == "ok"
        assert r.get("next_action")

    def test_kill_no_pipeline(self, mcp_project):
        # QA: nothing running -> killed=False; the next step must say
        # "nothing to stop", not "Pipeline stopped".
        r = run(awf.awf_kill(project_dir=str(mcp_project)))
        assert r["status"] == "ok"
        assert r.get("killed") is False
        na = r.get("next_action", "")
        assert na
        assert "Nothing to stop" in na or "nothing to stop" in na
