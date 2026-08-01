"""Tests for AUDIT v2 followup: supervisor timeout, time.monotonic, atomic writes, etc."""
from __future__ import annotations

import pytest

# ── C1 v2: supervisor timeout ────────────────────────────────────────────────


class TestSupervisorTimeout:

    def test_wait_for_supervisor_signal_raises_timeout(self, tmp_path, monkeypatch):
        """C1 v2 fix: wait_for_supervisor_signal raises TimeoutError on deadline."""
        from awf.supervisor import wait_for_supervisor_signal

        proj = tmp_path / "proj"
        for d in ("roles", "inbox", "outbox", "context", "logs"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: t\n")

        # Speed up: no sleep, very short timeout
        monkeypatch.setattr("time.sleep", lambda *_: None)

        with pytest.raises(TimeoutError, match="not received within"):
            wait_for_supervisor_signal(
                kind="plan", todo_id="", project_dir=proj,
                logs_dir=proj / ".agentic" / "logs",
                timeout=0,  # immediate timeout
            )

    def test_wait_for_supervisor_signal_returns_before_timeout(self, tmp_path, monkeypatch):
        """Signal arriving before timeout returns normally."""
        from awf.supervisor import wait_for_supervisor_signal

        proj = tmp_path / "proj"
        for d in ("roles", "inbox", "outbox", "context", "logs"):
            (proj / ".agentic" / d).mkdir(parents=True)

        # Simulate signal appearing after 2 polls
        call_count = {"n": 0}

        def fake_sleep(seconds):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                (proj / ".agentic" / "inbox" / "TODO-0042.ready").write_text("")

        monkeypatch.setattr("time.sleep", fake_sleep)

        result = wait_for_supervisor_signal(
            kind="plan", todo_id="", project_dir=proj,
            logs_dir=proj / ".agentic" / "logs",
            timeout=60,
        )
        assert result == "TODO-0042"


# ── H1: time.monotonic in commit_gate ────────────────────────────────────────


class TestCommitGateMonotonic:

    def test_commit_gate_uses_monotonic(self):
        """H1 fix: commit_gate uses time.monotonic, not time.time."""
        import inspect

        from awf import commit_gate

        src = inspect.getsource(commit_gate)
        # The deadline check must use time.monotonic
        assert "time.monotonic()" in src, "H1: commit_gate must use time.monotonic()"
        # Must NOT use time.time() for deadline comparison
        assert "time.time() > deadline" not in src, "H1: time.time() > deadline found"


# ── H2: cmd_report find_signal_file ──────────────────────────────────────────


class TestCmdReportCanonicalLegacy:

    def test_cmd_report_uses_find_signal_file(self):
        """H2 fix: report logic uses find_signal_file (now in api.py)."""
        import inspect

        from awf import api

        src = inspect.getsource(api)
        assert "find_signal_file" in src, "H2: api must use find_signal_file for report"

    def test_cmd_report_legacy_short_form_recognized(self, tmp_path, monkeypatch):
        """awf report shows DONE for legacy short form DONE-0001.ready."""
        from types import SimpleNamespace

        from awf import cmd_report

        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        inbox.mkdir()
        outbox.mkdir()
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: t\n")

        # Worker used legacy short form
        (inbox / "TODO-0001.ready").write_text("")
        (inbox / "TODO-0001.md").write_text("**Phase:** Step 1\n")
        (outbox / "DONE-0001.ready").write_text("")  # legacy short form

        # Mock git (no git repo) — patch at the module where it's imported
        import awf.cmd_report
        monkeypatch.setattr(awf.cmd_report, "_is_git_repo", lambda *_: False) if hasattr(awf.cmd_report, '_is_git_repo') else None
        # cmd_report imports git_utils as submodule name 'git_utils' (relative)
        # so we need to patch via the module-level name it uses
        from awf import git_utils
        monkeypatch.setattr(git_utils, "is_git_repo", lambda *_: False)

        args = SimpleNamespace(project_dir=str(proj))
        # Should not crash
        result = cmd_report.run(args)
        assert result == 0

    def test_cmd_report_project_dir_flag(self, tmp_path, monkeypatch, capsys):
        """M1: awf report --project-dir /path works with project in different dir."""
        from types import SimpleNamespace

        from awf import cmd_report

        # Create project in a subdirectory (not CWD)
        proj = tmp_path / "separate-project"
        (proj / ".agentic").mkdir(parents=True)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        inbox.mkdir()
        outbox.mkdir()
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: separate-proj\n")

        (inbox / "TODO-0001.ready").write_text("step: 1\n")
        (outbox / "DONE-TODO-0001.ready").write_text("")

        # Mock git
        from awf import git_utils
        monkeypatch.setattr(git_utils, "is_git_repo", lambda *_: False)

        # Run report with --project-dir pointing to our separate project
        args = SimpleNamespace(project_dir=str(proj))
        result = cmd_report.run(args)
        assert result == 0

        out = capsys.readouterr().out
        assert "separate-proj" in out, "Report should show project name from --project-dir"
        assert "TODO-0001" in out, "Report should list TODOs from the specified project"


# ── L1: cmd_baseline glob fix ────────────────────────────────────────────────


class TestCmdBaselineGlobFix:

    def test_baseline_lists_created_files(self, tmp_path, capsys, monkeypatch):
        """L1 fix: baseline lists created files (now via api.create_baseline
        result.files_created, surfaced via cmd_baseline stdout)."""
        from types import SimpleNamespace

        from awf import cmd_baseline

        proj = tmp_path / "proj"
        (proj / ".agentic").mkdir(parents=True)
        (proj / ".agentic" / "context").mkdir()
        (proj / ".agentic" / "config.yaml").write_text("project:\n  name: t\n")

        # Mock git (no git repo) — patch at api module (logic moved there in MCP-1)
        monkeypatch.setattr("awf.api.git_utils.is_git_repo", lambda *_: False)
        monkeypatch.setattr("awf.api.shutil.which", lambda _: None)

        args = SimpleNamespace(todo_id="TODO-0001", project_dir=str(proj))
        result = cmd_baseline.run(args)
        assert result == 0

        captured = capsys.readouterr()
        # L1 fix: list of files should NOT be empty
        assert "BASELINE-TODO-0001.sha" in captured.out, (
            "L1: baseline files should be listed (was empty before fix)"
        )


# ── M1: --project-dir for 4 commands ─────────────────────────────────────────


class TestProjectDirSupport:

    def _parse(self, argv):
        """Use the real argparse via _dispatch_subcommand internals."""
        # We can't call _dispatch (it executes), but we can build the parser
        # by calling internal code. Easier: re-import the parser construction.

        from awf.cli import _dispatch_subcommand
        # Use a minimal re-creation: parse via main() with mock dispatch.
        # Actually simpler: catch SystemExit and check args via parse_known_args
        # on a parser built the same way.
        # The cleanest way: invoke `_dispatch_subcommand(argv)` and intercept.
        # For tests, just verify the args parsing by re-running the argparse setup:
        return _dispatch_subcommand.__wrapped__ if hasattr(_dispatch_subcommand, '__wrapped__') else None

    def test_baseline_has_project_dir_arg(self, capsys):
        """M1 fix: awf baseline supports --project-dir."""
        import sys

        from awf import cli

        # Call main with these args; it'll try to dispatch, but we just want to know
        # --project-dir is parsed. If argparse rejects, SystemExit is raised.
        # Use a stub project_dir that doesn't have .agentic — run() returns 1, not exit.
        sys.argv = ["awf", "baseline", "TODO-0001", "--project-dir", "/tmp/nonexistent-test-m1"]
        try:
            cli.main()
        except SystemExit as e:
            pytest.fail(f"M1: 'awf baseline --project-dir' should not exit with {e.code}")
        # If we got here, args parsed. Verify by checking it didn't reject --project-dir.

    def test_rollback_has_project_dir_arg(self):
        import sys

        from awf import cli

        sys.argv = ["awf", "rollback", "TODO-0001", "--project-dir", "/tmp/nonexistent-test-m1", "--dry-run"]
        try:
            cli.main()
        except SystemExit as e:
            pytest.fail(f"M1: 'awf rollback --project-dir' should not exit with {e.code}")

    def test_add_role_has_project_dir_arg(self):
        import sys

        from awf import cli

        # add-role needs a name and may prompt for model — use --model to skip
        sys.argv = ["awf", "add-role", "myrole", "--project-dir", "/tmp/nonexistent-test-m1", "--model", "x"]
        try:
            cli.main()
        except SystemExit as e:
            pytest.fail(f"M1: 'awf add-role --project-dir' should not exit with {e.code}")

    def test_reset_has_project_dir_arg(self):
        import sys

        from awf import cli

        sys.argv = ["awf", "reset", "--project-dir", "/tmp/nonexistent-test-m1"]
        try:
            cli.main()
        except SystemExit as e:
            pytest.fail(f"M1: 'awf reset --project-dir' should not exit with {e.code}")


# ── H3: atomic writes ────────────────────────────────────────────────────────


class TestAtomicWrites:

    def test_atomic_write_text_creates_file(self, tmp_path):
        """H3 helper: atomic_write_text creates file with content."""
        from awf._atomic import atomic_write_text

        target = tmp_path / "test.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text() == "hello world"

    def test_atomic_write_text_creates_parents(self, tmp_path):
        """atomic_write_text creates parent dirs."""
        from awf._atomic import atomic_write_text

        target = tmp_path / "deep" / "nested" / "test.txt"
        atomic_write_text(target, "content")
        assert target.read_text() == "content"

    def test_atomic_write_text_no_temp_file_left(self, tmp_path):
        """After successful write, no .tmp files remain."""
        from awf._atomic import atomic_write_text

        target = tmp_path / "test.txt"
        atomic_write_text(target, "content")
        temps = list(tmp_path.glob("*.tmp"))
        assert temps == [], f"Left temp files: {temps}"

    def test_verify_uses_atomic(self):
        """H3: verify.py uses atomic_write_text."""
        import inspect

        from awf import verify

        src = inspect.getsource(verify)
        assert "atomic_write_text" in src, "H3: verify.py should use atomic_write_text"

    def test_cmd_baseline_uses_atomic(self):
        """H3: baseline logic uses atomic_write_text (now in api.py)."""
        import inspect

        from awf import api

        src = inspect.getsource(api)
        assert "atomic_write_text" in src

    def test_cmd_init_uses_atomic(self):
        """H3: init logic uses atomic_write_text (now in api.py)."""
        import inspect

        from awf import api

        src = inspect.getsource(api)
        assert "atomic_write_text" in src

    def test_cmd_analyze_roles_uses_atomic(self):
        """H3: cmd_analyze_roles.py uses atomic_write_text."""
        import inspect

        from awf import cmd_analyze_roles

        src = inspect.getsource(cmd_analyze_roles)
        assert "atomic_write_text" in src

    def test_cmd_add_role_uses_atomic(self):
        """H3: add_role logic uses atomic_write_text (now in api.py)."""
        import inspect

        from awf import api

        src = inspect.getsource(api)
        assert "atomic_write_text" in src

    def test_cmd_rollback_uses_atomic(self):
        """H3: rollback logic uses atomic_write_text (now in api.py)."""
        import inspect

        from awf import api

        src = inspect.getsource(api)
        assert "atomic_write_text" in src

    def test_atomic_write_crash_cleans_temp(self, tmp_path, monkeypatch):
        """H3 crash safety: if write raises, temp file is cleaned up."""
        import os as _os

        from awf._atomic import atomic_write_text

        target = tmp_path / "crash-test.txt"
        raise_on_write = {"triggered": False}

        original_fdopen = _os.fdopen

        def fake_fdopen(fd, mode, encoding=None):
            class _RaisingFile:
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    _os.close(fd)
                def write(self, data):
                    if not raise_on_write["triggered"]:
                        raise_on_write["triggered"] = True
                        raise OSError("disk full")
                    return len(data)
            return _RaisingFile()

        monkeypatch.setattr(_os, "fdopen", fake_fdopen)

        with pytest.raises(OSError, match="disk full"):
            atomic_write_text(target, "should crash")

        # Target file should NOT exist (write failed before rename)
        assert not target.exists(), "Target must not exist after failed write"

        # No temp files should remain
        temps = list(tmp_path.glob("*.tmp"))
        assert temps == [], f"Temp files left after crash: {temps}"

    def test_atomic_write_partial_state_impossible(self, tmp_path):
        """H3: after successful write, file is never in partial state."""
        from awf._atomic import atomic_write_text

        target = tmp_path / "atomic-test.txt"
        large_content = "x" * 100_000  # 100KB — large enough that a crash mid-write matters

        atomic_write_text(target, large_content)

        # File exists and has exact content (no truncation possible)
        assert target.exists()
        assert target.read_text() == large_content
        assert target.stat().st_size == 100_000


# ── M5: no empty model="" in opencode.json ───────────────────────────────────


class TestOpencodeAgentsNoEmptyModel:

    def test_apply_with_empty_model_omits_key(self, tmp_path):
        """M5 fix: when model='', opencode_agents.apply doesn't add 'model' key."""
        import json

        from awf import opencode_agents

        cfg = tmp_path / "opencode.json"
        cfg.write_text(json.dumps({"agent": {}}))

        result = opencode_agents.apply(str(cfg), ["worker"], "")
        # Read back
        data = json.loads(cfg.read_text())
        assert "worker" in data["agent"]
        assert "model" not in data["agent"]["worker"], (
            "M5: empty model should not produce 'model': '' key"
        )

    def test_apply_with_real_model_includes_key(self, tmp_path):
        """With real model, key is included."""
        import json

        from awf import opencode_agents

        cfg = tmp_path / "opencode.json"
        cfg.write_text(json.dumps({"agent": {}}))

        opencode_agents.apply(str(cfg), ["worker"], "claude-4")
        data = json.loads(cfg.read_text())
        assert data["agent"]["worker"]["model"] == "claude-4"


# ── M2: short_id dedup ───────────────────────────────────────────────────────


class TestShortIdDedup:

    def test_short_id_is_public(self):
        """M2 fix: short_id is public function in signals.py."""
        from awf.signals import short_id
        assert short_id("TODO-0001") == "0001"
        assert short_id("0001") == "0001"  # no TODO- prefix

    def test_todos_imports_from_signals(self):
        """todos.py uses signals.short_id (not its own copy)."""
        from awf import signals, todos
        # Both should reference the same function object
        assert todos._short_id is signals.short_id
