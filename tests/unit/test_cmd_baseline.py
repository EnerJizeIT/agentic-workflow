"""Unit tests for awf.cmd_baseline — shell injection safety.

H3 regression: after MCP-1 refactor, baseline logic lives in awf.api.
Tests patch awf.api.pipeline (not cmd_baseline) to verify the safety
property survives the move. AUD04-06/FU-10: test_cmd runs via
awf._proc.run_tree (process-group kill on timeout), so the test_cmd
calls are patched on ``awf.api.pipeline.run_tree``; python/pip env
snippets still go through ``awf.api.pipeline.subprocess.run``.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

from conftest import _git_init  # AUD12-08: shared git boilerplate

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
        _git_init(proj)
        return proj

    def test_test_cmd_runs_without_shell(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd from config runs via shlex.split, not shell=True.

        FU-10: test_cmd goes through awf._proc.run_tree (process-group
        kill), so we patch that, not the env-snippet subprocess.run.
        """
        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0001"})()

        with patch("awf.api.pipeline.run_tree") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=0, stdout="ok\n", stderr="",
            )
            cmd_baseline.run(args)

            for call in mock_run.call_args_list:
                call_args = call[0][0] if call[0] else None
                if call_args and isinstance(call_args, list) and call_args[0] == "/bin/true":
                    assert call[1].get("shell") is not True
                    break
            else:
                assert False, "No call found with /bin/true as list"

    def test_test_cmd_shell_injection_safe(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd with ; separator — split into args, not shell injection.

        FU-10: patch run_tree (the test_cmd path), not subprocess.run.
        """
        proj = self._setup(tmp_path)
        agentic = proj / ".agentic"
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: /bin/true; /bin/echo injected\n"
        )
        monkeypatch.chdir(proj)

        args = type("Args", (), {"todo_id": "TODO-0002"})()

        with patch("awf.api.pipeline.run_tree") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=1, stdout="", stderr=""
            )
            cmd_baseline.run(args)

            # shlex.split turns `;` into a separator: the injection fragment
            # becomes a distinct argv element, never a shell string.
            expected = shlex.split("/bin/true; /bin/echo injected")
            assert mock_run.called, "run_tree not called for test_cmd"
            cmd_arg = mock_run.call_args_list[0][0][0]
            assert cmd_arg == expected, f"test_cmd not shlex-split: {cmd_arg!r}"
            assert mock_run.call_args_list[0][1].get("shell") is not True

    def test_empty_test_cmd_skipped(self, tmp_path: Path, monkeypatch) -> None:
        """Empty test_cmd → no subprocess call for it."""
        proj = self._setup(tmp_path)
        agentic = proj / ".agentic"
        (agentic / "config.yaml").write_text(
            "verification:\n  test_cmd: \"   \"\n"
        )
        monkeypatch.chdir(proj)

        args = type("Args", (), {"todo_id": "TODO-0003"})()

        with patch("awf.api.pipeline.run_tree") as mock_tree, \
                patch("awf.api.pipeline.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], returncode=0, stdout="", stderr=""
            )
            cmd_baseline.run(args)

            assert not mock_tree.called, "empty test_cmd must not run any command"
            for call in mock_run.call_args_list:
                call_args = call[0][0] if call[0] else None
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
        _git_init(proj)
        return proj

    def test_test_cmd_timeout_records_failure(self, tmp_path: Path, monkeypatch) -> None:
        """test_cmd hangs → test_status='failed', baseline still created.

        FU-10: test_cmd runs via run_tree — patch that, env snippets keep
        using the real subprocess.run.
        """
        from awf.api import pipeline as api_pipeline

        proj = self._setup(tmp_path)
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": "TODO-0008"})()

        def fake_run_tree(cmd, *a, **kw):
            if isinstance(cmd, list) and cmd and "pytest" in str(cmd[0]):
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 300))
            return subprocess.CompletedProcess([], returncode=0, stdout="", stderr="")

        monkeypatch.setattr(api_pipeline, "run_tree", fake_run_tree)
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

        def spy_run_tree(cmd, *a, **kw):
            if isinstance(cmd, list) and cmd and "pytest" in str(cmd[0]):
                captured_timeouts.append(("test_cmd", kw.get("timeout")))
            return subprocess.CompletedProcess([], returncode=0, stdout="", stderr="")

        def spy_run(cmd, *a, **kw):
            if isinstance(cmd, list) and "pip" in str(cmd):
                captured_timeouts.append(("pip_list", kw.get("timeout")))
            return original_run(cmd, *a, **kw)

        monkeypatch.setattr("awf.api.pipeline.run_tree", spy_run_tree)
        monkeypatch.setattr("awf.api.pipeline.subprocess.run", spy_run)
        cmd_baseline.run(args)

        # Both must have non-None timeouts (was None before fix).
        names = {name for name, _ in captured_timeouts}
        assert "test_cmd" in names, "test_cmd call not captured"
        assert "pip_list" in names, "pip list call not captured"
        for name, t in captured_timeouts:
            assert t is not None and t > 0, f"{name} missing timeout (was None before fix)"


class TestBaselineCmd:
    """U1 (TODO-0011): ``verification.baseline_cmd`` — fast smoke command for
    the dispatch-time baseline instead of the full test_cmd.

    Absent/empty/blank ``baseline_cmd`` → legacy behavior (test_cmd).
    """

    def _setup(self, tmp_path: Path, verification_yaml: str) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        agentic = proj / ".agentic"
        (agentic / "context").mkdir(parents=True)
        (agentic / "config.yaml").write_text(verification_yaml)
        _git_init(proj)
        return proj

    def _run(self, proj: Path, monkeypatch, todo_id: str) -> int:
        monkeypatch.chdir(proj)
        args = type("Args", (), {"todo_id": todo_id})()
        return cmd_baseline.run(args)

    def _tests_log(self, proj: Path, todo_id: str) -> str:
        return (proj / ".agentic" / "context" / f"BASELINE-{todo_id}.tests.log").read_text()

    def test_baseline_cmd_used_when_set(self, tmp_path: Path, monkeypatch) -> None:
        """baseline_cmd set → its marker lands in the tests log, test_cmd not run."""
        proj = self._setup(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n"
            "  baseline_cmd: /bin/echo U1-SMOKE-MARKER\n",
        )
        rc = self._run(proj, monkeypatch, "TODO-0020")
        assert rc == 0
        log = self._tests_log(proj, "TODO-0020")
        assert "U1-SMOKE-MARKER" in log
        assert "FULL-SUITE-RUN" not in log

    def test_fallback_to_test_cmd_when_absent(self, tmp_path: Path, monkeypatch) -> None:
        """No baseline_cmd key → test_cmd runs (legacy behavior)."""
        proj = self._setup(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n",
        )
        rc = self._run(proj, monkeypatch, "TODO-0021")
        assert rc == 0
        log = self._tests_log(proj, "TODO-0021")
        assert "FULL-SUITE-RUN" in log
        assert "U1-SMOKE-MARKER" not in log

    def test_fallback_to_test_cmd_when_empty(self, tmp_path: Path, monkeypatch) -> None:
        """baseline_cmd: '' → test_cmd runs."""
        proj = self._setup(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n"
            "  baseline_cmd: ''\n",
        )
        rc = self._run(proj, monkeypatch, "TODO-0022")
        assert rc == 0
        log = self._tests_log(proj, "TODO-0022")
        assert "FULL-SUITE-RUN" in log

    def test_fallback_to_test_cmd_when_blank(self, tmp_path: Path, monkeypatch) -> None:
        """baseline_cmd: '   ' (whitespace) → test_cmd runs."""
        proj = self._setup(
            tmp_path,
            "verification:\n"
            "  test_cmd: /bin/echo FULL-SUITE-RUN\n"
            "  baseline_cmd: '   '\n",
        )
        rc = self._run(proj, monkeypatch, "TODO-0023")
        assert rc == 0
        log = self._tests_log(proj, "TODO-0023")
        assert "FULL-SUITE-RUN" in log
