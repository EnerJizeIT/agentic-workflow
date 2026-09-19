"""Tests for continue_pipeline resume (DF5-2) and salvage in awf_status (DF5-4).

DF5-2: continue_pipeline reads state file stage_name → resumes from correct
       stage, not stage 0. Was: always from_stage=None → stage_idx=0.

DF5-4: awf_status surfaces salvage_needed from state file — supervisor
       sees salvage context without reading background logs.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from awf import api
from awf.api.lifecycle import get_status
from awf.api.pipeline import continue_pipeline
from awf.pipeline_state import write_state


@pytest.fixture
def project(tmp_git_repo):
    """Project with .agentic/ + pipeline + active TODO."""
    api.init_project(tmp_git_repo, project_name="Test")

    # Minimal pipeline
    pipes = tmp_git_repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "stages:\n"
        '  - name: plan\n    role: supervisor\n    kind: plan\n'
        '  - name: implement\n    role: worker\n    kind: execute\n'
        '  - name: verify\n    role: supervisor\n    kind: verify\n',
        encoding="utf-8",
    )

    # Worker role
    roles = tmp_git_repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute TODO.\n", encoding="utf-8")

    # Active TODO
    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub task\n")
    (inbox / "TODO-0001.ready").write_text("")

    return tmp_git_repo


class TestContinuePipelineResume:
    """DF5-2: continue_pipeline reads state for from_stage."""

    def test_resumes_from_state_stage_name(self, project, monkeypatch):
        """State has stage_name='implement' → from_stage='implement'."""
        # Write state as if pipeline crashed at implement stage
        write_state(
            project,
            stage_idx=1,
            stage_name="implement",
            stage_kind="execute",
            todo_id="TODO-0001",
        )

        # Mock run_pipeline to capture args (foreground mode)
        captured_args: list = []

        def mock_run_pipeline(args):
            captured_args.append(args)
            return 0

        with patch("awf.orchestrator.run_pipeline", mock_run_pipeline):
            result = continue_pipeline(project, background=False)

        assert result.run_mode == "foreground"
        assert result.exit_code == 0
        assert len(captured_args) == 1
        assert captured_args[0].from_stage == "implement", (
            f"Expected from_stage='implement' from state file, "
            f"got {captured_args[0].from_stage}"
        )

    def test_explicit_from_stage_overrides_state(self, project):
        """If from_stage given explicitly, state file is ignored."""
        write_state(project, stage_name="implement")

        captured_args: list = []

        def mock_run_pipeline(args):
            captured_args.append(args)
            return 0

        with patch("awf.orchestrator.run_pipeline", mock_run_pipeline):
            continue_pipeline(project, from_stage="verify", background=False)

        assert captured_args[0].from_stage == "verify"

    def test_no_state_starts_from_beginning(self, project):
        """No state file → from_stage stays None → pipeline starts from 0."""
        state_file = project / ".agentic" / "state" / "current.yaml"
        if state_file.exists():
            state_file.unlink()

        captured_args: list = []

        def mock_run_pipeline(args):
            captured_args.append(args)
            return 0

        with patch("awf.orchestrator.run_pipeline", mock_run_pipeline):
            continue_pipeline(project, background=False)

        assert captured_args[0].from_stage is None

    def test_refuses_if_pipeline_already_running(self, project):
        """DF5-6: if PID alive → noop."""
        import os
        write_state(project, pipeline_pid=os.getpid(), stage_name="implement")
        result = continue_pipeline(project, background=False)
        assert result.run_mode == "noop"
        assert "already running" in result.message.lower()

    def test_noop_without_active_todo(self, project):
        """No active TODO → noop."""
        inbox = project / ".agentic" / "inbox"
        (inbox / "TODO-0001.ready").unlink()
        result = continue_pipeline(project, background=False)
        assert result.run_mode == "noop"

    def test_background_mode_launches_subprocess(self, project, monkeypatch):
        """background=True → launches detached subprocess (not synchronous)."""
        write_state(project, stage_name="implement", stage_kind="execute")

        captured: dict = {}

        class _FakeProc:
            pid = 54321

            def __init__(self, args, **kwargs):
                captured["argv"] = list(args)

        from awf.api import _background
        monkeypatch.setattr(_background.subprocess, "Popen", _FakeProc)
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda pid, log_file=None: True)

        result = continue_pipeline(project, background=True)

        assert result.run_mode == "background"
        assert result.run_id == 54321
        assert "implement" in result.message


class TestContinueReconcile:
    """DF6-2: reconcile runs before continue_pipeline."""

    def test_reconcile_clears_stale_pid_before_continue(self, project):
        """Stale PID in state → reconcile clears → continue proceeds."""
        write_state(project, pipeline_pid="999999", stage_name="implement")

        captured_args: list = []

        def mock_run_pipeline(args):
            captured_args.append(args)
            return 0

        with patch("awf.orchestrator.run_pipeline", mock_run_pipeline):
            result = continue_pipeline(project, background=False)

        # Stale PID cleared → pipeline runs (not noop)
        assert result.run_mode == "foreground"
        assert captured_args[0].from_stage == "implement"


class TestSalvageInStatus:
    """DF5-4: awf_status surfaces salvage_needed from state file."""

    def test_status_shows_salvage_needed(self, project):
        """State with salvage_needed=True → status includes it."""
        write_state(
            project,
            salvage_needed=True,
            salvage_stage="agent-implementer",
            stage_name="agent-implementer",
            stage_kind="execute",
            todo_id="TODO-0001",
        )

        result = get_status(project)
        assert result.salvage_needed is True
        assert result.salvage_stage == "agent-implementer"

    def test_status_without_salvage(self, project):
        """No salvage in state → salvage_needed=False."""
        write_state(project, stage_name="plan", stage_kind="plan")

        result = get_status(project)
        assert result.salvage_needed is False

    def test_status_salvage_expected_action(self, project):
        """Salvage state → expected_action contains 'Salvage'."""
        write_state(
            project,
            salvage_needed=True,
            salvage_stage="agent-implementer",
            stage_name="agent-implementer",
            stage_kind="execute",
            todo_id="TODO-0001",
        )

        result = get_status(project)
        assert result.expected_action is not None
        assert "Salvage" in result.expected_action or "salvage" in result.expected_action.lower()

    def test_status_salvage_overrides_worker_running_action(self, project):
        """B5 (dogfood-11): salvage + ALIVE orchestrator → salvage action.

        The orchestrator process being alive (waiting for the supervisor)
        used to yield "worker stage running — auto-transition on worker
        DONE. Do NOT intervene", contradicting salvage_needed: true.
        """
        import os

        logs = project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "awf-start.pid").write_text(f"{os.getpid()}\n")
        write_state(
            project,
            salvage_needed=True,
            salvage_stage="agent-implementer",
            salvage_count=3,
            stage_name="agent-implementer",
            stage_kind="execute",
            todo_id="TODO-0001",
            pipeline_pid=os.getpid(),
        )

        result = get_status(project)
        assert result.pipeline_running is True
        assert "Salvage" in result.expected_action
        assert "attempt 3" in result.expected_action
        assert "worker stage running" not in result.expected_action.lower()

    def test_status_salvage_serializable(self, project):
        """StatusResult with salvage fields is JSON-serializable."""
        import json
        write_state(
            project,
            salvage_needed=True,
            salvage_stage="agent-dev",
            stage_name="agent-dev",
            stage_kind="execute",
        )

        result = get_status(project)
        d = result.as_dict()
        json.dumps(d)  # should not raise


class TestPidReuseDefense:
    """QA .14 (NEG-2026-09-19): identity check BEFORE liveness.

    The old order (os.kill → /proc read, with an 'assume ours' fallback when
    the read failed) let a reused PID pass as our pipeline. Now the cmdline
    check is the first gate and an unreadable /proc means 'not ours'.
    """

    def test_unreadable_cmdline_is_not_our_pipeline(self, project, monkeypatch):
        import os

        from awf.api import pipeline as api_pipeline

        write_state(project, pipeline_pid=os.getpid())
        monkeypatch.setattr(api_pipeline, "_read_pid_cmdline", lambda pid: None)

        assert api_pipeline._is_pipeline_running(project) is None

    def test_foreign_cmdline_is_not_our_pipeline(self, project, monkeypatch):
        import os

        from awf.api import pipeline as api_pipeline

        write_state(project, pipeline_pid=os.getpid())
        monkeypatch.setattr(
            api_pipeline, "_read_pid_cmdline", lambda pid: "nginx: worker process\x00",
        )

        assert api_pipeline._is_pipeline_running(project) is None

    def test_alive_own_pipeline_returned(self, project, monkeypatch):
        import os

        from awf.api import pipeline as api_pipeline

        write_state(project, pipeline_pid=os.getpid())
        monkeypatch.setattr(
            api_pipeline, "_read_pid_cmdline", lambda pid: "python\x00-m\x00awf\x00start",
        )

        assert api_pipeline._is_pipeline_running(project) == os.getpid()
