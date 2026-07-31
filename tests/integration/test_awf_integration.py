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
        monkeypatch.setattr(commit_gate, "APPROVE_TIMEOUT_SECONDS", 1)
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

        monkeypatch.setattr(commit_gate, "APPROVE_TIMEOUT_SECONDS", 5)
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


# ── _run_in_background argv reconstruction (H6) ──────────────────────────────


class TestRunInBackgroundArgv:
    """H6 fix: ``_run_in_background`` must rebuild argv without ``--background``
    but preserve all other flags — including ``--project-dir`` in BOTH
    space-separated (``--project-dir /x``) and ``=value`` (``--project-dir=/x``)
    forms. Failure mode: child launched in wrong CWD, or child re-launches
    itself in a loop (if ``--background`` is not stripped).
    """

    def _capture(self, monkeypatch):
        """Patch subprocess.Popen to capture child_argv instead of spawning."""
        captured: dict = {}

        class _CapturingPopen:
            pid = 12345

            def __init__(self, args, **kwargs):
                captured["argv"] = list(args)
                captured["cwd"] = kwargs.get("cwd")
                captured["start_new_session"] = kwargs.get("start_new_session")

        # Patch the open() call too — _run_in_background opens a log file
        from awf import cmd_start

        monkeypatch.setattr(cmd_start.subprocess, "Popen", _CapturingPopen)
        # Avoid real file writes
        monkeypatch.setattr("builtins.open", lambda *a, **kw: _NullFile())
        return captured

    def test_background_stripped_project_dir_preserved_equal_form(
        self, tmp_path, monkeypatch
    ):
        """--project-dir=/path form must survive argv rebuild."""
        from awf import cmd_start

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["awf", "start", "--background", "--project-dir=" + str(tmp_path)],
        )

        args = SimpleNamespace(
            command="start",
            background=True,
            project_dir=str(tmp_path),
            pipeline=None,
            from_stage=None,
            auto=False,
            timeout=3600,
        )
        rc = cmd_start._run_in_background(args)
        assert rc == 0

        argv = captured["argv"]
        assert "--background" not in argv, "H6: --background must be stripped"
        assert any(
            a.startswith("--project-dir=") for a in argv
        ), "H6: --project-dir= form must be preserved"
        assert captured["cwd"] == str(tmp_path.resolve())

    def test_background_stripped_project_dir_space_form(self, tmp_path, monkeypatch):
        """--project-dir /path (space-separated) form must survive rebuild."""
        from awf import cmd_start

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["awf", "start", "--background", "--project-dir", str(tmp_path)],
        )

        args = SimpleNamespace(
            command="start",
            background=True,
            project_dir=str(tmp_path),
            pipeline=None,
            from_stage=None,
            auto=False,
            timeout=3600,
        )
        rc = cmd_start._run_in_background(args)
        assert rc == 0

        argv = captured["argv"]
        assert "--background" not in argv
        # Both flag and its value must be present, in order
        assert "--project-dir" in argv
        idx = argv.index("--project-dir")
        assert argv[idx + 1] == str(tmp_path)

    def test_other_flags_preserved(self, tmp_path, monkeypatch):
        """--auto, --pipeline, --from-stage, --timeout must survive rebuild."""
        from awf import cmd_start

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            [
                "awf", "start", "--background",
                "--auto", "--pipeline", "release",
                "--from-stage", "verify",
                "--timeout", "600",
                "--project-dir=" + str(tmp_path),
            ],
        )

        args = SimpleNamespace(
            command="start", background=True, project_dir=str(tmp_path),
            pipeline="release", from_stage="verify", auto=True, timeout=600,
        )
        rc = cmd_start._run_in_background(args)
        assert rc == 0

        argv = captured["argv"]
        assert "--background" not in argv
        assert "--auto" in argv
        assert "--pipeline" in argv
        assert argv[argv.index("--pipeline") + 1] == "release"
        assert "--from-stage" in argv
        assert argv[argv.index("--from-stage") + 1] == "verify"
        assert "--timeout" in argv
        assert argv[argv.index("--timeout") + 1] == "600"

    def test_no_duplicate_project_dir_when_already_present(
        self, tmp_path, monkeypatch
    ):
        """If user passed --project-dir, awf must NOT add a second one."""
        from awf import cmd_start

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["awf", "start", "--background", "--project-dir=" + str(tmp_path)],
        )

        args = SimpleNamespace(
            command="start", background=True, project_dir=str(tmp_path),
            pipeline=None, from_stage=None, auto=False, timeout=3600,
        )
        rc = cmd_start._run_in_background(args)
        assert rc == 0

        argv = captured["argv"]
        # Count --project-dir occurrences (both forms)
        equal_count = sum(1 for a in argv if a.startswith("--project-dir="))
        space_count = sum(1 for a in argv if a == "--project-dir")
        assert equal_count == 1, "Exactly one --project-dir= must be present"
        assert space_count == 0, "No space-form --project-dir when = form used"

    def test_project_dir_added_when_missing(self, tmp_path, monkeypatch):
        """If user did NOT pass --project-dir, awf must inject it."""
        from awf import cmd_start

        captured = self._capture(monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["awf", "start", "--background", "--auto"],
        )

        args = SimpleNamespace(
            command="start", background=True, project_dir=str(tmp_path),
            pipeline=None, from_stage=None, auto=True, timeout=3600,
        )
        rc = cmd_start._run_in_background(args)
        assert rc == 0

        argv = captured["argv"]
        assert "--project-dir" in argv
        idx = argv.index("--project-dir")
        assert argv[idx + 1] == str(tmp_path.resolve())


class _NullFile:
    """File-like object that discards writes (used to mock log file open)."""

    def write(self, *_args, **_kwargs):
        return 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
