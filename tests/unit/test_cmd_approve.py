"""Unit tests for awf.cmd_approve."""
from argparse import Namespace
from pathlib import Path

from awf import cmd_approve


class TestCmdApprove:

    def test_creates_approve_signal(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        args = Namespace(todo_id="TODO-0002", project_dir=str(project_dir))
        ret = cmd_approve.run(args)

        assert ret == 0
        signal = project_dir / ".agentic" / "inbox" / "APPROVE-TODO-0002.ready"
        assert signal.exists()

    def test_missing_todo_id_returns_1(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        args = Namespace(todo_id="", project_dir=str(project_dir))
        ret = cmd_approve.run(args)

        assert ret == 1

    def test_creates_inbox_dir_if_missing(self, tmp_path: Path) -> None:
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        args = Namespace(todo_id="TODO-0005", project_dir=str(project_dir))
        ret = cmd_approve.run(args)

        assert ret == 0
        inbox = project_dir / ".agentic" / "inbox"
        assert inbox.is_dir()
        assert (inbox / "APPROVE-TODO-0005.ready").exists()
