"""Unit tests for awf.paths — path resolution functions."""
from pathlib import Path

from awf.paths import (
    agentic_dir,
    config_file,
    context_dir,
    dashboards_dir,
    done_dir,
    handoff_dir,
    inbox,
    inputs_dir,
    logs_dir,
    outbox,
    phases_dir,
    pipelines_dir,
    roles_dir,
    state_dir,
)


class TestPaths:

    def test_agentic_dir_default(self, tmp_path: Path) -> None:
        (tmp_path / ".agentic").mkdir()
        result = agentic_dir(tmp_path)
        assert result == tmp_path / ".agentic"

    def test_agentic_dir_resolves(self, tmp_path: Path) -> None:
        subdir = tmp_path / "sub" / "dir"
        subdir.mkdir(parents=True)
        result = agentic_dir(subdir)
        assert result.name == ".agentic"
        assert result.parent == subdir

    def test_inbox(self, tmp_path: Path) -> None:
        assert inbox(tmp_path) == tmp_path / ".agentic" / "inbox"

    def test_outbox(self, tmp_path: Path) -> None:
        assert outbox(tmp_path) == tmp_path / ".agentic" / "outbox"

    def test_context_dir(self, tmp_path: Path) -> None:
        assert context_dir(tmp_path) == tmp_path / ".agentic" / "context"

    def test_config_file(self, tmp_path: Path) -> None:
        assert config_file(tmp_path) == tmp_path / ".agentic" / "config.yaml"

    def test_config_file_name(self, tmp_path: Path) -> None:
        cf = config_file(tmp_path)
        assert cf.name == "config.yaml"
        assert cf.suffix == ".yaml"

    # AUD01-07: the missing resolvers (roles/logs/state/pipelines/phases/
    # dashboards/inputs) — new cases for the new resolvers.
    def test_done_dir(self, tmp_path: Path) -> None:
        assert done_dir(tmp_path) == tmp_path / ".agentic" / "done"

    def test_handoff_dir(self, tmp_path: Path) -> None:
        assert handoff_dir(tmp_path) == tmp_path / ".agentic" / "handoff"

    def test_roles_dir(self, tmp_path: Path) -> None:
        assert roles_dir(tmp_path) == tmp_path / ".agentic" / "roles"

    def test_logs_dir(self, tmp_path: Path) -> None:
        assert logs_dir(tmp_path) == tmp_path / ".agentic" / "logs"

    def test_state_dir(self, tmp_path: Path) -> None:
        assert state_dir(tmp_path) == tmp_path / ".agentic" / "state"

    def test_pipelines_dir(self, tmp_path: Path) -> None:
        assert pipelines_dir(tmp_path) == tmp_path / ".agentic" / "pipelines"

    def test_phases_dir(self, tmp_path: Path) -> None:
        assert phases_dir(tmp_path) == tmp_path / ".agentic" / "phases"

    def test_dashboards_dir(self, tmp_path: Path) -> None:
        assert dashboards_dir(tmp_path) == tmp_path / ".agentic" / "dashboards"

    def test_inputs_dir(self, tmp_path: Path) -> None:
        assert inputs_dir(tmp_path) == tmp_path / ".agentic" / "inputs"

    def test_paths_are_resolved(self, tmp_path: Path) -> None:
        """Paths use .resolve() so relative inputs become absolute."""
        import os
        old = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = agentic_dir(".")
            assert result.is_absolute()
        finally:
            os.chdir(old)
