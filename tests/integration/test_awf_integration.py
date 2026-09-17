"""Integration tests covering gaps flagged by QA-report:

- BD-22: snapshot-based stale-filter for ``watch_new_glob`` (the
  ``watch_paths`` variant is already covered in test_orchestrator.py).
- APPROVE timeout path in commit_gate.py (auto=True, no signal arrives).
- ``_run_in_background`` argv reconstruction (H6 fix is regression-prone:
  stripping ``--background`` while keeping ``--project-dir`` in both
  space-separated and ``=value`` forms).

These run awf modules through their public API (no bin/awf subprocess),
but exercise multiple modules together — hence ``tests/integration/``.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

# ── BD-22: snapshot filter for watch_new_glob ────────────────────────────────


class _FakePopen:
    """Minimal Popen stub: never exits on its own (caller raises TimeoutError)."""

    pid = 1
    _poll_count = 0

    def __init__(self, *args, **kwargs):
        type(self)._poll_count = 0

    def poll(self):
        type(self)._poll_count += 1
        return None  # always "still running"

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class TestBD22SnapshotWatchNewGlob:
    """BD-22 snapshot filter applied to ``watch_new_glob``.

    A pre-existing file matching the glob pattern must NOT trigger the
    signal — only a NEW file appearing during subprocess execution counts.
    """

    def test_pre_existing_glob_files_are_ignored(self, tmp_path, monkeypatch):
        """Stale TODO-*.ready in inbox must not falsely signal supervisor done."""
        from awf.signal_watch import run_subprocess_until_signal

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FakePopen)
        monkeypatch.setattr("awf.signal_watch.time.sleep", lambda *_a, **_kw: None)

        inbox = tmp_path / "inbox"
        inbox.mkdir()
        # Pre-existing stale TODO from previous run
        (inbox / "TODO-0001.ready").write_text("")
        (inbox / "TODO-0002.ready").write_text("")

        # With snapshot, these stale files should NOT trigger signal.
        # hard_timeout=1 forces fast failure if snapshot works correctly.
        with pytest.raises(TimeoutError, match="did not produce signal"):
            run_subprocess_until_signal(
                cmd=["opencode", "run"],
                cwd=tmp_path,
                watch_new_glob=(inbox, "TODO-*.ready"),
                logs_dir=None,
                hard_timeout=1,
            )

    def test_new_glob_file_triggers_signal(self, tmp_path, monkeypatch):
        """A TODO-*.ready appearing DURING execution is a real signal."""
        from awf.signal_watch import run_subprocess_until_signal

        class _TriggeringPopen(_FakePopen):
            def poll(self):
                # On second poll, create a NEW todo file then keep "running"
                # — but since signal is detected, watch will return on next
                # iteration once natural exit is observed.
                if type(self)._poll_count == 1:
                    (inbox_dir / "TODO-0099.ready").write_text("")
                type(self)._poll_count += 1
                return None

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _TriggeringPopen)

        # Force natural exit after signal detected by patching poll to return 0
        # on the 3rd call.
        original_poll = _TriggeringPopen.poll

        def poll_then_exit(self):
            rc = original_poll(self)
            if type(self)._poll_count >= 3:
                return 0  # natural exit
            return rc

        monkeypatch.setattr(_TriggeringPopen, "poll", poll_then_exit)
        monkeypatch.setattr("awf.signal_watch.time.sleep", lambda *_a, **_kw: None)

        result = run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_new_glob=(inbox_dir, "TODO-*.ready"),
            logs_dir=None,
            hard_timeout=10,
        )
        assert result.returncode == 0
        assert (inbox_dir / "TODO-0099.ready").exists()

    def test_signal_holder_captures_fired_filename(self, tmp_path, monkeypatch):
        """Auditor Idea 1: signal_watch populates signal_holder with the
        exact filename that fired (e.g. 'TODO-0042.ready'). Lets callers
        avoid re-globbing inbox and picking a stale TODO by mtime.
        """
        from awf.signal_watch import run_subprocess_until_signal

        class _TriggeringPopen(_FakePopen):
            def poll(self):
                if type(self)._poll_count == 1:
                    (inbox_dir / "TODO-0042.ready").write_text("")
                type(self)._poll_count += 1
                if type(self)._poll_count >= 3:
                    return 0
                return None

        inbox_dir = tmp_path / "inbox"
        inbox_dir.mkdir()
        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _TriggeringPopen)
        monkeypatch.setattr("awf.signal_watch.time.sleep", lambda *_a, **_kw: None)

        holder: dict[str, str] = {}
        result = run_subprocess_until_signal(
            cmd=["opencode", "run"],
            cwd=tmp_path,
            watch_new_glob=(inbox_dir, "TODO-*.ready"),
            logs_dir=None,
            hard_timeout=10,
            signal_holder=holder,
        )
        assert result.returncode == 0
        assert holder.get("signal") == "TODO-0042.ready", (
            "signal_holder must contain exact filename that fired"
        )

    def test_signal_holder_empty_when_no_signal(self, tmp_path, monkeypatch):
        """signal_holder stays empty when hard_timeout fires (no signal arrived)."""
        from awf.signal_watch import run_subprocess_until_signal

        monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _FakePopen)
        monkeypatch.setattr("awf.signal_watch.time.sleep", lambda *_a, **_kw: None)

        holder: dict[str, str] = {}
        with pytest.raises(TimeoutError):
            run_subprocess_until_signal(
                cmd=["opencode", "run"],
                cwd=tmp_path,
                watch_paths=[tmp_path / "never_exists.ready"],
                logs_dir=None,
                hard_timeout=1,
                signal_holder=holder,
            )
        assert holder == {}


# ── APPROVE timeout path ─────────────────────────────────────────────────────


class TestApproveTimeout:
    """commit_gate.maybe_commit(auto=True) must raise TimeoutError when
    neither APPROVE-{todo}.ready nor ACK-{todo}.ready arrives in time.

    Regression-prone: APPROVE_TIMEOUT_SECONDS default is 1800s. Tests must
    patch it to a small value to fail fast.
    """

    def test_timeout_when_no_signal_arrives(self, tmp_path, monkeypatch):
        from awf import commit_gate

        # Fast timeout for the test
        monkeypatch.setattr(commit_gate, "_get_approve_timeout", lambda: 1)
        monkeypatch.setattr(commit_gate, "APPROVE_POLL_INTERVAL", 0)

        # Pretend we're in a git repo (skip is_git_repo check)
        monkeypatch.setattr(commit_gate.git_utils, "is_git_repo", lambda *_: True)

        # .agentic structure with empty inbox (no APPROVE/ACK signal)
        agentic = tmp_path / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "logs").mkdir(parents=True)

        with pytest.raises(TimeoutError, match="APPROVE/ACK signal not received"):
            commit_gate.maybe_commit(
                stage_name="verify",
                todo_id="TODO-0001",
                policy="commit_and_next",
                project_dir=tmp_path,
                logs_dir=agentic / "logs",
                auto=True,
                baseline_sha="",  # irrelevant — we timeout before commit
            )

    def test_signal_arrives_within_window(self, tmp_path, monkeypatch):
        """Positive path: APPROVE signal file appears during the wait loop.
        maybe_commit must exit the loop and proceed (we short-circuit the
        actual commit via is_git_repo=False after the loop)."""
        from awf import commit_gate

        monkeypatch.setattr(commit_gate, "_get_approve_timeout", lambda: 5)
        monkeypatch.setattr(commit_gate, "APPROVE_POLL_INTERVAL", 0)

        monkeypatch.setattr(commit_gate.git_utils, "is_git_repo", lambda *_: True)

        agentic = tmp_path / ".agentic"
        inbox = agentic / "inbox"
        inbox.mkdir(parents=True)
        logs_dir = agentic / "logs"
        logs_dir.mkdir(parents=True)

        # Create APPROVE file on the first sleep call — by the time the next
        # loop iteration runs approve_signal.exists() returns True.
        approve_path = inbox / "APPROVE-TODO-0001.ready"

        def _create_on_first_sleep(*_a, **_kw):
            approve_path.write_text("")

        monkeypatch.setattr(commit_gate.time, "sleep", _create_on_first_sleep)

        # After wait loop exits, block the actual commit (skip is_git_repo
        # check by returning False so the function returns early post-wait).
        # We patch via a wrapper that checks if the APPROVE file exists yet.
        real_is_git = commit_gate.git_utils.is_git_repo

        def is_git_after_signal(_):
            # Once APPROVE exists, the wait loop has exited — refuse to be
            # a git repo so maybe_commit returns without committing.
            return not approve_path.exists()

        monkeypatch.setattr(commit_gate.git_utils, "is_git_repo", is_git_after_signal)

        # Should NOT raise — signal arrived.
        commit_gate.maybe_commit(
            stage_name="verify",
            todo_id="TODO-0001",
            policy="commit_and_next",
            project_dir=tmp_path,
            logs_dir=logs_dir,
            auto=True,
            baseline_sha="",
        )
        assert approve_path.exists(), "Signal file must have been created"


# ── Background start (unified through api._start_in_background) ──────────


class TestBackgroundStart:
    """Background start goes through api.start_pipeline(background=True) —
    single source of truth (was: legacy _run_in_background in cmd_start).

    Tests verify BEHAVIOR (PID file + log file + Popen called with correct
    argv), not argv reconstruction internals. H6 fix preserved as a
    behavior test: --background must NOT be in child argv (would loop).
    """

    def _capture_popen(self, monkeypatch):
        """Patch subprocess.Popen at the api._background module (where the
        background launcher lives after MCP-audit package split).

        Also patches _verify_child_alive (DF5-10) so tests don't do a real
        1-second sleep + os.kill on a fake PID.
        """
        captured: dict = {}

        class _CapturingPopen:
            pid = 12345

            def __init__(self, args, **kwargs):
                captured["argv"] = list(args)
                captured["cwd"] = kwargs.get("cwd")
                captured["start_new_session"] = kwargs.get("start_new_session")

        from awf.api import _background
        monkeypatch.setattr(_background.subprocess, "Popen", _CapturingPopen)
        # DF5-10: mock child liveness check — fake Popen PID is not real
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda pid, log_file=None: True)
        return captured

    def _setup_project(self, tmp_path):
        """Minimal .agentic/ structure required by api.start_pipeline."""
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        (proj / ".agentic" / "config.yaml").write_text(
            'project:\n  name: test\n'
        )
        # Dogfood-10: start_pipeline background guard requires active TODO
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task")
        return proj

    def test_background_writes_pid_file(self, tmp_path, monkeypatch):
        """awf start --background writes .agentic/logs/awf-start.pid (MCP-4)."""
        proj = self._setup_project(tmp_path)
        self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        rc = cmd_start_run(args)
        assert rc == 0

        pid_file = proj / ".agentic" / "logs" / "awf-start.pid"
        assert pid_file.exists(), "PID file must be written for status polling"
        assert pid_file.read_text().strip() == "12345"  # mocked pid

    def test_background_creates_log_file(self, tmp_path, monkeypatch):
        """awf start --background creates .agentic/logs/awf-start.out."""
        proj = self._setup_project(tmp_path)
        self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        rc = cmd_start_run(args)
        assert rc == 0

        log_file = proj / ".agentic" / "logs" / "awf-start.out"
        assert log_file.exists()

    def test_background_strips_background_flag_from_child(self, tmp_path, monkeypatch):
        """H6 regression: --background must NOT appear in child argv (would loop).

        Now checked at the api layer (was: cmd_start._run_in_background).
        """
        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        cmd_start_run(args)

        argv = captured["argv"]
        assert "--background" not in argv, (
            "H6: child must not receive --background (would cause infinite loop)"
        )

    def test_background_preserves_project_dir_in_child(self, tmp_path, monkeypatch):
        """--project-dir must reach the child subprocess (correct cwd)."""
        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        cmd_start_run(args)

        argv = captured["argv"]
        # project_dir must appear (either form)
        assert any(
            a == "--project-dir" or a.startswith("--project-dir=") for a in argv
        ), "project_dir must be passed to child"
        assert captured["cwd"] == str(proj.resolve())

    def test_background_preserves_other_flags(self, tmp_path, monkeypatch):
        """--auto, --pipeline, --from-stage, --timeout reach the child."""
        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline="release", from_stage="verify", auto=True, timeout=600,
        )
        cmd_start_run(args)

        argv = captured["argv"]
        assert "--auto" in argv
        assert "--pipeline" in argv
        assert argv[argv.index("--pipeline") + 1] == "release"
        assert "--from-stage" in argv
        assert argv[argv.index("--from-stage") + 1] == "verify"
        assert "--timeout" in argv
        assert argv[argv.index("--timeout") + 1] == "600"

    def test_background_prints_pid_and_log_path(self, tmp_path, monkeypatch, capsys):
        """User-facing output: PID + log file path printed for monitoring."""
        proj = self._setup_project(tmp_path)
        self._capture_popen(monkeypatch)

        from awf.cmd_start import run as cmd_start_run
        args = SimpleNamespace(
            command="start", background=True, project_dir=str(proj),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        cmd_start_run(args)

        captured = capsys.readouterr()
        assert "PID 12345" in captured.out
        assert "awf-start.out" in captured.out


class _NullFile:
    """File-like object that discards writes (legacy: kept for any tests
    that still reference it; was used to mock log file open in pre-unification
    _run_in_background tests)."""

    def write(self, *_args, **_kwargs):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── dogfood-11: worker log append (B4) ───────────────────────────────────────


class TestWorkerLogAppend:
    """Retried stages must APPEND to the worker log, not overwrite it.

    Before: ``open(..., "w")`` erased the previous run's output, so
    post-mortems of a five-retry stage lost every earlier answer.
    """

    def test_two_runs_append_with_markers(self, tmp_path):
        from awf.signal_watch import run_subprocess_until_signal

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        for i in (1, 2):
            run_subprocess_until_signal(
                cmd=["bash", "-c", f"echo payload-run-{i}"],
                cwd=tmp_path,
                watch_paths=[],
                logs_dir=logs_dir,
                hard_timeout=30,
            )

        log = (logs_dir / "worker-output.out").read_text(encoding="utf-8")
        # Both runs' output survives (append semantics).
        assert "payload-run-1" in log
        assert "payload-run-2" in log
        # Run boundaries are marked for forensics.
        assert log.count("===== awf run ") == 2
