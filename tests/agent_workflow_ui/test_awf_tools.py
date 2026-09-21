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
        monkeypatch.setattr(api, "run_brief", lambda *a, **kw: None)

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
        monkeypatch.setattr(api, "run_brief", lambda *a, **kw: None)

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
        monkeypatch.setattr(api, "run_brief", lambda *a, **kw: None)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "verify"
        assert "awf_approve" in result["next_action"]
        assert "Continue the run loop" not in result["next_action"]

    def test_blocked_event_gets_blocked_next_action(self, monkeypatch):
        """AUD08-01 regression: a blocked event must get the unblock
        instruction, not the stuck 'timeout' one."""
        monkeypatch.setattr(api, "wait_for_event", self._fake_wait_with("blocked"))
        monkeypatch.setattr(api, "run_brief", lambda *a, **kw: None)

        result = asyncio.run(awf.awf_wait_for_event(project_dir="/tmp", timeout=10))

        assert result["event_type"] == "blocked"
        assert "Worker blocked" in result["next_action"]
        assert "Continue the run loop" not in result["next_action"]


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
        from awf import run_state

        run_state.write_run(
            mcp_project, active=True, queue=[self.T], started_at=run_state.now_iso(),
        )
        result = run(awf.awf_approve(
            self.T, project_dir=str(mcp_project),
            evidence="pytest -q → 348 passed; verdict: approve",
        ))
        assert result["status"] == "ok"
        assert "awf_run_next" in result["next_action"]
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

    def test_params_are_proxied(self, monkeypatch):
        calls: list[dict] = []

        def spy_metrics(project_dir, **kw):
            calls.append(kw)
            raise api.AwfApiError("spy: stop")

        monkeypatch.setattr(api, "collect_metrics", spy_metrics)

        result = run(
            awf.awf_metrics(
                project_dir="/tmp", refresh_subscriptions=True, mirror=False
            )
        )

        assert result["status"] == "error"
        assert calls and calls[0]["refresh_subscriptions"] is True
        assert calls[0]["mirror"] is False
