"""Unit tests for awf.cmd_baseline — shell injection safety.

H3 regression: after MCP-1 refactor, baseline logic lives in awf.api.
Tests patch awf.api.pipeline.subprocess (not cmd_baseline) to verify the safety
property survives the move.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from awf import cmd_baseline


class TestBaselineShellSafety:

    def _setup(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "context").mkdir(parents=True)
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: /bin/true\n"
        )
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
        (proj / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
        return proj

    def test_test_cmd_runs_without_shell(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd from config runs via shlex.split, not shell=True."""
        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0001"})()

        with patch("awf.api.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=0, stdout="ok\n", stderr="",
            )
            cmd_baseline.run(args)

            for call in mock_run.call_args_list:
                call_args = call[1].get("args", call[0][0] if call[0] else None)
                if call_args and isinstance(call_args, list) and call_args[0] == "/bin/true":
                    assert call[1].get("shell") is not True
                    break
            else:
                assert False, "No call found with /bin/true as list"

    def test_test_cmd_shell_injection_safe(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd with ; separator — treated as argument, not shell injection."""
        proj = self._setup(tmp_path)
        agentic = proj / ".agentic"
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: /bin/true; /bin/echo injected\n"
        )
        monkeypatch.chdir(proj)

        args = type("Args", (), {"todo_id": "TODO-0002"})()

        with patch("awf.api.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=1, stdout="", stderr=""
            )
            cmd_baseline.run(args)

            for call in mock_run.call_args_list:
                call_args = call[1].get("args", call[0][0] if call[0] else None)
                if call_args and isinstance(call_args, list) and len(call_args) > 1:
                    if "; /bin/echo injected" in call_args:
                        assert call[1].get("shell") is not True
                        break

    def test_empty_test_cmd_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Empty test_cmd → no subprocess call for it."""
        proj = self._setup(tmp_path)
        agentic = proj / ".agentic"
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: \"   \"\n"
        )
        monkeypatch.chdir(proj)

        args = type("Args", (), {"todo_id": "TODO-0003"})()

        with patch("awf.api.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=0, stdout="", stderr=""
            )
            cmd_baseline.run(args)

            for call in mock_run.call_args_list:
                call_args = call[1].get("args", call[0][0] if call[0] else None)
                if call_args and isinstance(call_args, list):
                    assert call_args[0] != "/bin/true"

    def test_python_version_uses_list(self, tmp_path: Path, monkeypatch) -> None:
        """Hardcoded python --version uses list form, not shell."""
        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0004"})()

        with patch("awf.api.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=0, stdout="Python 3.12\n", stderr=""
            )
            cmd_baseline.run(args)

            found = False
            for call in mock_run.call_args_list:
                call_args = call[1].get("args", call[0][0] if call[0] else None)
                if call_args and isinstance(call_args, list) and "--version" in call_args:
                    assert call[1].get("shell") is not True
                    found = True
                    break
            assert found, "No call found with --version"


class TestBaselineTimeout:
    """QA 2026-08-03: hung test_cmd / pip list must not block baseline creation.

    Before fix: ``subprocess.run`` had no timeout in ``create_baseline``.
    A hung watcher (`pytest --watch`) or stdin-prompt froze the whole
    baseline snapshot indefinitely.
    """

    def _setup(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "context").mkdir(parents=True)
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: pytest --watch\n"
        )
        subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
        (proj / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
        return proj

    def test_test_cmd_timeout_records_failure(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd hangs → test_status='failed', baseline still created."""
        from awf.api import pipeline as api_pipeline

        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0008"})()

        real_run = subprocess.run
        call_count = {"n": 0}

        def fake_run(cmd, *a, **kw):
            call_count["n"] += 1
            # First subprocess.run call is `git rev-parse HEAD` (no timeout kwarg).
            # Second is test_cmd — raise TimeoutExpired.
            # Subsequent calls (git status, pip list) — pass through.
            if isinstance(cmd, list) and cmd and "pytest" in str(cmd[0]):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 300))
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(api_pipeline.subprocess, "run", fake_run)
        result = cmd_baseline.run(args)

        # Baseline returns 0 even on test failure (baseline itself succeeded).
        assert result == 0
        # test_status should be 'failed' (logged timeout), not 'passed'.
        tests_log = (proj / ".agentic" / "context" / "BASELINE-TODO-0008.tests.log").read_text()
        assert "timed out" in tests_log.lower()

    def test_timeout_kwarg_passed_to_subprocess(self, tmp_path: Path, monkeypatch) -> None:
        """Verify timeout= kwarg is actually passed for test_cmd and pip list."""
        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0007"})()

        captured_timeouts = []
        original_run = subprocess.run

        def spy_run(cmd, *a, **kw):
            if isinstance(cmd, list) and "pip" in str(cmd):
                captured_timeouts.append(("pip_list", kw.get("timeout")))
            elif isinstance(cmd, list) and "pytest" in str(cmd[0]):
                captured_timeouts.append(("test_cmd", kw.get("timeout")))
            return original_run(cmd, *a, **kw)

        monkeypatch.setattr(
            "awf.api.pipeline.subprocess.run", spy_run
        )
        cmd_baseline.run(args)

        # Both must have non-None timeouts (was None before fix).
        names = {name for name, _ in captured_timeouts}
        assert "test_cmd" in names, "test_cmd call not captured"
        assert "pip_list" in names, "pip list call not captured"
        for name, t in captured_timeouts:
            assert t is not None and t > 0, f"{name} missing timeout (was None before fix)"
