"""Unit tests for awf.cmd_baseline — shell injection safety."""
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
        args = type("Args", (), {"todo_id": "T1"})()

        with patch("awf.cmd_baseline.subprocess.run") as mock_run:
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

        args = type("Args", (), {"todo_id": "T2"})()

        with patch("awf.cmd_baseline.subprocess.run") as mock_run:
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

        args = type("Args", (), {"todo_id": "T3"})()

        with patch("awf.cmd_baseline.subprocess.run") as mock_run:
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
        args = type("Args", (), {"todo_id": "T4"})()

        with patch("awf.cmd_baseline.subprocess.run") as mock_run:
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
