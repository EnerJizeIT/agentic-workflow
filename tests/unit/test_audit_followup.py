"""Tests for audit-followup fixes (C1, M1, H2, H3, H4, H6, H7)."""
from __future__ import annotations

import subprocess

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


class TestH6ProjectDirEqualsForm:
    """H6 fix: --project-dir=/path form was duplicated in background mode."""

    def test_equals_form_not_duplicated(self, tmp_path, monkeypatch):
        """--project-dir=/path in argv should NOT cause second --project-dir add."""
        from awf import cmd_start

        captured: dict = {}

        class _FakeProc:
            def __init__(self, args_list, **kwargs):
                captured["argv"] = args_list
                self.pid = 1

        from types import SimpleNamespace
        args = SimpleNamespace(
            command="start", project_dir=str(tmp_path),
            background=True, auto=False, pipeline="default",
            from_stage=None, timeout=3600,
        )
        monkeypatch.setattr(cmd_start.sys, "argv",
                            ["awf", "start", "--background", f"--project-dir={tmp_path}"])
        monkeypatch.setattr(cmd_start.subprocess, "Popen", _FakeProc)

        cmd_start._run_in_background(args)

        child_argv = captured["argv"]
        # Count --project-dir occurrences (either form)
        project_dir_count = sum(
            1 for a in child_argv
            if a == "--project-dir" or a.startswith("--project-dir=")
        )
        assert project_dir_count == 1, (
            f"H6: expected exactly 1 --project-dir, got {project_dir_count} in {child_argv}"
        )

    def test_space_form_not_duplicated(self, tmp_path, monkeypatch):
        """--project-dir /path (space-separated) also not duplicated."""
        from awf import cmd_start

        captured: dict = {}

        class _FakeProc:
            def __init__(self, args_list, **kwargs):
                captured["argv"] = args_list
                self.pid = 1

        from types import SimpleNamespace
        args = SimpleNamespace(
            command="start", project_dir=str(tmp_path),
            background=True, auto=False, pipeline="default",
            from_stage=None, timeout=3600,
        )
        monkeypatch.setattr(cmd_start.sys, "argv",
                            ["awf", "start", "--background", "--project-dir", str(tmp_path)])
        monkeypatch.setattr(cmd_start.subprocess, "Popen", _FakeProc)

        cmd_start._run_in_background(args)

        child_argv = captured["argv"]
        project_dir_count = sum(
            1 for a in child_argv
            if a == "--project-dir" or a.startswith("--project-dir=")
        )
        assert project_dir_count == 1


# ── H7: SIGTERM returns real returncode ───────────────────────────────────────


class TestH7SigtermReturnCode:

    def test_grace_terminate_returns_nonzero(self, tmp_path, monkeypatch):
        """H7 fix: signal_watch returns real returncode after SIGTERM (was 0)."""
        from awf.signal_watch import run_subprocess_until_signal

        class _HangingPopen:
            def __init__(self, cmd, cwd=None, env=None):
                self.cmd = cmd
                self.pid = 999
                self.returncode = None  # hangs forever

            def poll(self):
                # First poll: trigger signal detection
                (tmp_path / "DONE-TODO-0001.ready").write_text("")
                return None  # still running

            def terminate(self):
                self.returncode = -15  # SIGTERM

            def kill(self):
                self.returncode = -9

            def wait(self, timeout=None):
                return self.returncode

        monkeypatch.setattr("subprocess.Popen", _HangingPopen)
        monkeypatch.setattr("time.sleep", lambda *_: None)

        result = run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_paths=[tmp_path / "DONE-TODO-0001.ready"],
            logs_dir=None,
            grace_seconds=0,  # immediate terminate
        )
        assert result.returncode != 0, (
            f"H7: SIGTERM'd process must return non-zero, got {result.returncode}"
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
