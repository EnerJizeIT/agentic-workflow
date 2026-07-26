"""Unit tests for awf.paths — path resolution functions."""
from pathlib import Path

from awf.paths import agentic_dir, config_file, context_dir, inbox, outbox


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
