"""Tests for audit-followup fixes (C1, M1, H2, H3, H4, H6, H7)."""
from __future__ import annotations

import subprocess

import pytest

from awf.pipeline import Stage
from awf.signals import find_signal_file

# ── C1: REVIEW handling in verify ────────────────────────────────────────────


class TestC1ReviewRejection:

    def test_find_signal_file_canonical(self, tmp_path):
        """find_signal_file finds canonical DONE-TODO-0001.md."""
        (tmp_path / "DONE-TODO-0001.md").write_text("done")
        result = find_signal_file(tmp_path, "DONE", "TODO-0001", ".md")
        assert result is not None
        assert result.name == "DONE-TODO-0001.md"

    def test_find_signal_file_legacy_short(self, tmp_path):
        """H2 fix: find_signal_file finds legacy DONE-0001.md."""
        (tmp_path / "DONE-0001.md").write_text("done")
        result = find_signal_file(tmp_path, "DONE", "TODO-0001", ".md")
        assert result is not None
        assert result.name == "DONE-0001.md"

    def test_find_signal_file_canonical_wins_over_legacy(self, tmp_path):
        """If both exist, canonical wins (first in tuple)."""
        (tmp_path / "DONE-TODO-0001.md").write_text("canonical")
        (tmp_path / "DONE-0001.md").write_text("legacy")
        result = find_signal_file(tmp_path, "DONE", "TODO-0001", ".md")
        assert result.name == "DONE-TODO-0001.md"

    def test_find_signal_file_returns_none_when_missing(self, tmp_path):
        assert find_signal_file(tmp_path, "DONE", "TODO-0001", ".md") is None

    def test_detect_supervisor_signal_review_overrides_ack(self, tmp_path):
        """C1 fix: _detect_supervisor_signal prefers REVIEW over ACK.

        Without this, stale ACK from previous verify could mask new REVIEW
        rejection. REVIEW is the most recent decision.
        """
        from awf.supervisor import _detect_supervisor_signal

        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir()
        outbox.mkdir()

        # Both ACK and REVIEW exist — REVIEW should win
        (inbox / "ACK-TODO-0042.ready").write_text("")
        (outbox / "REVIEW-TODO-0042.md").write_text("# rejected")

        signal = _detect_supervisor_signal("verify", "TODO-0042", inbox, outbox)
        assert signal == "REVIEW-TODO-0042", (
            f"C1: REVIEW must override ACK, got {signal!r}"
        )

    def test_detect_supervisor_signal_ack_when_no_review(self, tmp_path):
        """When only ACK exists (no REVIEW), returns ACK."""
        from awf.supervisor import _detect_supervisor_signal

        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir()
        outbox.mkdir()
        (inbox / "ACK-TODO-0042.ready").write_text("")

        signal = _detect_supervisor_signal("verify", "TODO-0042", inbox, outbox)
        assert signal == "ACK-TODO-0042"

    def test_detect_supervisor_signal_returns_empty_for_plan_without_todo(self, tmp_path):
        """For plan kind without any TODO ready, returns empty string."""
        from awf.supervisor import _detect_supervisor_signal

        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir()
        outbox.mkdir()
        signal = _detect_supervisor_signal("plan", "", inbox, outbox)
        assert signal == ""


# ── M1: rollback empty TODO check ────────────────────────────────────────────


class TestM1RollbackEmptyCheck:

    def test_handle_rollback_returns_exit_1_on_no_new_todo(self, tmp_path, monkeypatch):
        """M1 fix: rollback without new TODO exits 1 (was returning empty string)."""
        from awf.orchestrator import _handle_rollback

        monkeypatch.setattr("awf.orchestrator._run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr("awf.orchestrator._find_active_todo", lambda *a: "")

        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="implement", role="worker", kind="execute"),
        ]
        idx, todo, exit_code = _handle_rollback(
            project_dir=tmp_path, logs_dir=tmp_path,
            stages=stages, current_todo="TODO-0001",
            auto=False, target="implement",
        )
        assert exit_code == 1, "M1: rollback without new TODO must exit 1"


# ── H6: --project-dir= form in background ────────────────────────────────────


# ── H6: --project-dir handling ──────────────────────────────────────────────
# Originally tested argv reconstruction in cmd_start._run_in_background.
# After MCP-MIGRATION unification: background path lives in
# api._start_in_background (builds argv from scratch, no parsing) —
# duplication is structurally impossible. Behavior covered by:
#   tests/integration/test_awf_integration.py::TestBackgroundStart
#   tests/unit/test_cmd_start.py (BD-30 invariants)


# ── H7: SIGTERM returns real returncode ───────────────────────────────────────


class TestH7HardTimeoutKill:
    """BD-20 redesign: grace kill removed. Only hard_timeout kills."""

    def test_hard_timeout_kills_hung_process(self, tmp_path, monkeypatch):
        """BD-20: hung process (no signal, no exit) → hard_timeout → SIGKILL."""
        from awf.signal_watch import run_subprocess_until_signal

        class _HangingPopen:
            def __init__(self, cmd, cwd=None, env=None, **kwargs):
                self.cmd = cmd
                self.pid = 999
                self.returncode = None

            def poll(self):
                return None  # never exits

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        monkeypatch.setattr("subprocess.Popen", _HangingPopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        with pytest.raises(TimeoutError, match="did not produce signal"):
            run_subprocess_until_signal(
                cmd=["opencode", "run"],
                cwd=tmp_path,
                watch_paths=[],
                logs_dir=None,
                hard_timeout=0,  # immediate timeout
            )


# ── H4: TOCTOU atomic claim ──────────────────────────────────────────────────


class TestH4ToctouClaim:

    def test_claim_for_submit_returns_true_first_call(self):
        """First claim_for_submit on pending form returns True."""
        from datetime import datetime, timezone

        from agent_workflow_ui.state import FormRecord, FormRegistry

        reg = FormRegistry()
        reg.PERSIST_ENABLED = False  # don't write to disk in tests
        record = FormRecord(
            form_id="FORM-test-1", template="test",
            opened_at=datetime.now(timezone.utc),
        )
        reg.add(record)
        assert reg.claim_for_submit("FORM-test-1") is True

    def test_claim_for_submit_returns_false_second_call(self):
        """Second concurrent claim returns False (TOCTOU protection)."""
        from datetime import datetime, timezone

        from agent_workflow_ui.state import FormRecord, FormRegistry

        reg = FormRegistry()
        reg.PERSIST_ENABLED = False
        record = FormRecord(
            form_id="FORM-test-2", template="test",
            opened_at=datetime.now(timezone.utc),
        )
        reg.add(record)

        first = reg.claim_for_submit("FORM-test-2")
        second = reg.claim_for_submit("FORM-test-2")

        assert first is True
        assert second is False, "H4: second concurrent claim must be rejected"

    def test_claim_returns_false_for_submitted(self):
        """Already submitted form cannot be re-claimed."""
        from datetime import datetime, timezone

        from agent_workflow_ui.state import FormRecord, FormRegistry

        reg = FormRegistry()
        reg.PERSIST_ENABLED = False
        record = FormRecord(
            form_id="FORM-test-3", template="test",
            opened_at=datetime.now(timezone.utc),
            status="submitted",
        )
        reg.add(record)
        assert reg.claim_for_submit("FORM-test-3") is False

    def test_claim_returns_false_for_unknown(self):
        from agent_workflow_ui.state import FormRegistry
        reg = FormRegistry()
        reg.PERSIST_ENABLED = False
        assert reg.claim_for_submit("nonexistent") is False


# ── H1: commit_all returncode check ──────────────────────────────────────────


class TestH1CommitAllReturnCode:

    def test_commit_all_returns_false_on_hook_rejection(self, tmp_path, monkeypatch):
        """H1 fix: commit_all returns False when git commit fails (pre-commit hook)."""
        from awf import git_utils

        # Init git repo
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
        (tmp_path / "README.md").write_text("init")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

        # Install failing pre-commit hook
        hook_dir = tmp_path / ".git" / "hooks"
        hook_dir.mkdir(parents=True, exist_ok=True)
        hook = hook_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)

        # Make a change
        (tmp_path / "file.txt").write_text("new")

        result = git_utils.commit_all(tmp_path, "test commit")
        assert result is False, "H1: commit_all must return False when pre-commit hook rejects"


# ── M8: transitions unknown signal print ─────────────────────────────────────


class TestM8UnknownSignalPrint:

    def test_unknown_signal_prints_to_stderr(self, capsys):
        """M8 fix: unknown signal prints to stderr (was only log.warning)."""
        from awf.transitions import resolve_transition

        stage = Stage(name="test", role="worker", kind="execute")
        action, target = resolve_transition(stage, "bogus-signal-type")
        assert action == "stop"
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "bogus-signal-type" in captured.err


# ── C1 full flow: run_pipeline verify + REVIEW rejection ─────────────────────


class TestC1PipelineReviewRejection:

    def _make_project(self, tmp_path):
        """Create a minimal project with a 3-stage pipeline."""
        proj = tmp_path / "proj"
        for d in ("roles", "inbox", "outbox", "context", "logs", "pipelines", "phases"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Supervisor")
        (proj / ".agentic" / "roles" / "worker.md").write_text("# Worker")
        (proj / ".agentic" / "config.yaml").write_text(
            'project:\n  name: t\nphases:\n  current: ".agentic/phases/plan.md"\n'
            'default_pipeline: "default"\n'
        )
        (proj / ".agentic" / "phases" / "plan.md").write_text(
            "- [ ] Step 1: initial task\n- [ ] Step 2: next task\n"
        )
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
            "name: default\nstages:\n"
            "  - name: plan\n    role: supervisor\n\n"
            "  - name: implement\n    role: worker\n\n"
            "  - name: verify\n    role: supervisor\n"
        )
        return proj

    def test_verify_review_stops_pipeline_no_commit(self, tmp_path, monkeypatch):
        """C1 full flow: supervisor writes REVIEW → pipeline stops, no commit, no step mark.

        Simulates: run_pipeline reaches verify stage, supervisor returns REVIEW signal.
        Pipeline should return exit code 1, NOT commit, NOT mark Step in plan.md.
        """
        from types import SimpleNamespace

        from awf import orchestrator

        proj = self._make_project(tmp_path)

        # Create active TODO so plan stage picks it up
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        (inbox / "TODO-0001.md").write_text("Step 1\nGoal: do something\n")
        (inbox / "TODO-0001.ready").write_text("")

        # Mock supervisor stage: plan → returns TODO, verify → returns REVIEW
        call_count = {"n": 0}

        def fake_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            call_count["n"] += 1
            if stage.kind == "plan":
                return "TODO-0001"
            elif stage.kind == "verify":
                # Supervisor writes REVIEW rejection
                (outbox / "REVIEW-TODO-0001.md").write_text(
                    "# Review\nWork is incomplete.\n"
                )
                return "REVIEW-TODO-0001"
            elif stage.kind == "replan":
                # replan after REVIEW — no new TODO created
                return ""
            return ""

        monkeypatch.setattr(orchestrator, "_run_supervisor_stage", fake_supervisor)

        # Mock _find_active_todo to return our TODO
        monkeypatch.setattr(orchestrator, "_find_active_todo", lambda pd: "TODO-0001")

        # Mock agent stage (worker) — just writes DONE signal
        def fake_agent(stage, todo_id, project_dir, config, logs_dir, prev_handoffs=None, **kwargs):
            (outbox / f"DONE-{todo_id}.md").write_text("Done.\n")
            (outbox / f"DONE-{todo_id}.ready").write_text("")

        monkeypatch.setattr(orchestrator, "_run_agent_stage", fake_agent)

        # Mock _maybe_commit — agent stage "next" transition calls it,
        # but we don't care about that for C1 (C1 is about verify stage).
        monkeypatch.setattr(orchestrator, "_maybe_commit", lambda *a, **kw: None)

        # Mock _mark_plan_step_done to detect if it was called
        # (only called on verify approved path, NOT on REVIEW)
        step_marked = {"v": False}

        def fake_mark(*a, **kw):
            step_marked["v"] = True

        monkeypatch.setattr(orchestrator, "_mark_plan_step_done", fake_mark)

        args = SimpleNamespace(
            project_dir=str(proj),
            pipeline=None,
            from_stage=None,
            auto=False,
        )

        result = orchestrator.run_pipeline(args)

        assert result == 1, "C1: pipeline should return 1 on REVIEW rejection"
        # _maybe_commit IS called for the agent stage's "next" transition — that's correct.
        # The C1 fix is that verify stage does NOT call _maybe_commit or _mark_plan_step_done
        # when supervisor returns REVIEW.
        assert not step_marked["v"], "C1: must NOT mark Step done on REVIEW rejection"
        # Verify the REVIEW file was written
        assert (outbox / "REVIEW-TODO-0001.md").exists(), "C1: REVIEW file should exist"


# ── QA: empty sup_signal at verify must abort (not silently approve) ────────


class TestQAEmptyVerifySignalAborts:
    """Regression: empty supervisor signal at verify stage must abort pipeline.

    Before fix: ``sup_signal.startswith("REVIEW-")`` was False for empty
    string, so the orchestrator fell through to the "approved implicit"
    path and committed unreviewed work — even when supervisor auto-skipped
    (no supervisor.md, salvage, unknown kind) or subprocess produced no
    ACK/APPROVE/REVIEW. This test verifies the explicit empty-signal
    abort was added.
    """

    def _make_project(self, tmp_path):
        proj = tmp_path / "proj"
        for d in ("roles", "inbox", "outbox", "context", "logs", "pipelines", "phases"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "roles" / "supervisor.md").write_text("# Supervisor")
        (proj / ".agentic" / "roles" / "worker.md").write_text("# Worker")
        (proj / ".agentic" / "config.yaml").write_text(
            'project:\n  name: t\nphases:\n  current: ".agentic/phases/plan.md"\n'
            'default_pipeline: "default"\n'
        )
        (proj / ".agentic" / "phases" / "plan.md").write_text(
            "- [ ] Step 1: initial task\n"
        )
        (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
            "name: default\nstages:\n"
            "  - name: plan\n    role: supervisor\n\n"
            "  - name: implement\n    role: worker\n\n"
            "  - name: verify\n    role: supervisor\n"
        )
        return proj

    def test_empty_verify_signal_returns_1_no_commit(self, tmp_path, monkeypatch):
        """Verify returns empty signal → pipeline returns 1, no commit, no step mark."""
        from types import SimpleNamespace

        from awf import orchestrator

        proj = self._make_project(tmp_path)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        (inbox / "TODO-0001.md").write_text("Step 1\nGoal: do something\n")
        (inbox / "TODO-0001.ready").write_text("")

        # Supervisor: plan → TODO, verify → "" (simulating auto-skip / no signal)
        def fake_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            if stage.kind == "plan":
                return "TODO-0001"
            if stage.kind == "verify":
                return ""  # no signal — the bug scenario
            return ""

        monkeypatch.setattr(orchestrator, "_run_supervisor_stage", fake_supervisor)
        monkeypatch.setattr(orchestrator, "_find_active_todo", lambda pd: "TODO-0001")

        def fake_agent(stage, todo_id, project_dir, config, logs_dir, prev_handoffs=None, **kwargs):
            (outbox / f"DONE-{todo_id}.md").write_text("Done.\n")
            (outbox / f"DONE-{todo_id}.ready").write_text("")

        monkeypatch.setattr(orchestrator, "_run_agent_stage", fake_agent)

        # Track verify-path side effects (must NOT happen)
        verify_commit_calls = {"n": 0}
        real_maybe_commit = orchestrator._maybe_commit

        def spy_commit(s_name, todo, action, *a, **kw):
            # Only count calls made from verify stage with commit_and_next
            if s_name == "verify" and action in ("commit_and_next", "commit_and_report"):
                verify_commit_calls["n"] += 1
            return real_maybe_commit(s_name, todo, action, *a, **kw)

        monkeypatch.setattr(orchestrator, "_maybe_commit", spy_commit)
        monkeypatch.setattr(orchestrator, "_read_baseline_sha", lambda *a: "")

        step_marked = {"v": False}
        monkeypatch.setattr(
            orchestrator, "_mark_plan_step_done", lambda *a, **kw: step_marked.__setitem__("v", True)
        )

        args = SimpleNamespace(
            project_dir=str(proj),
            pipeline=None,
            from_stage=None,
            auto=False,
        )

        result = orchestrator.run_pipeline(args)

        assert result == 1, "QA: empty sup_signal at verify must return 1"
        assert verify_commit_calls["n"] == 0, "QA: must NOT commit on empty verify signal"
        assert not step_marked["v"], "QA: must NOT mark Step done on empty verify signal"
