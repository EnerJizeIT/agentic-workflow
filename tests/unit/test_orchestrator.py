"""Unit tests for awf.orchestrator — role file resolution."""
from pathlib import Path

import pytest

from awf.orchestrator import _global_roles_dir, _resolve_role_file


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
