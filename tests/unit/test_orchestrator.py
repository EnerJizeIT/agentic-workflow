"""Unit tests for awf.orchestrator — role file resolution."""
from pathlib import Path

import pytest

from awf.orchestrator import _global_roles_dir, _maybe_commit, _resolve_role_file


class TestResolveRoleFile:

    def test_project_role_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "worker.md").write_text("global worker")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("worker", project_dir)
        assert result == project_dir / ".agentic" / "roles" / "worker.md"

    def test_falls_back_to_global(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)
        (project_dir / ".agentic" / "roles" / "worker.md").write_text("project worker")

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "auditor.md").write_text("global auditor")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("auditor", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "auditor.md"

    def test_raises_when_not_found_anywhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".agentic" / "roles").mkdir(parents=True)

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_role_file("nonexistent", project_dir)

        msg = str(exc_info.value)
        assert "nonexistent" in msg
        assert str(project_dir) in msg
        assert str(fake_home) in msg

    def test_global_only_no_project_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        fake_home = tmp_path / "home"
        (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
        (fake_home / ".config" / "awf" / "roles" / "system-analysis.md").write_text("global")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        result = _resolve_role_file("system-analysis", project_dir)
        assert result == fake_home / ".config" / "awf" / "roles" / "system-analysis.md"


class TestGlobalRolesDir:

    def test_returns_expected_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert _global_roles_dir() == fake_home / ".config" / "awf" / "roles"


class TestMaybeCommitBD8:

    def _init_git(self, tmp_path: Path) -> Path:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()
        (project_dir / "file.txt").write_text("hello")
        import subprocess
        subprocess.run(["git", "init"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "a@b.c"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "A"], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=project_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=project_dir, capture_output=True, check=True)
        return project_dir

    def test_maybe_commit_auto_waits_for_approve_signal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # Pre-create the approve signal so polling exits immediately
        (inbox / "APPROVE-TODO-0001.ready").touch()

        # Create a change to commit
        (project_dir / "file.txt").write_text("modified")

        sleep_calls = []
        monkeypatch.setattr(
            "time.sleep", lambda _d: sleep_calls.append(_d)
        )

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=True,
        )

        # Polling should have seen the signal on first check
        assert len(sleep_calls) == 0
        result = project_dir.joinpath(".git").joinpath("HEAD").read_text().strip()
        assert "refs/heads/master" in result or "refs/heads/main" in result

    def test_maybe_commit_auto_no_signal_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)
        inbox = project_dir / ".agentic" / "inbox"
        inbox.mkdir(parents=True)

        # No approve signal — monkeypatch sleep to raise after first call
        call_count = [0]
        def fail_after_one(_d):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise InterruptedError("test timeout")
        monkeypatch.setattr("time.sleep", fail_after_one)

        (project_dir / "file.txt").write_text("modified")

        with pytest.raises(InterruptedError):
            _maybe_commit(
                "verify", "TODO-0001", "commit_and_next",
                project_dir, logs_dir, auto=True,
            )

        # Verify no commit was made — file.txt still modified but not committed
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" in status.stdout

    def test_maybe_commit_non_auto_commits_immediately(self, tmp_path: Path) -> None:
        project_dir = self._init_git(tmp_path)
        logs_dir = project_dir / ".agentic" / "logs"
        logs_dir.mkdir(parents=True)

        (project_dir / "file.txt").write_text("modified")

        _maybe_commit(
            "verify", "TODO-0001", "commit_and_next",
            project_dir, logs_dir, auto=False,
        )

        # Verify commit was made — no uncommitted changes
        import subprocess
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=project_dir, capture_output=True, text=True,
        )
        assert "file.txt" not in status.stdout
