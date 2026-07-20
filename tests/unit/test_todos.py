"""Unit tests for awf.todos — is_closed, has_progress, list_active_todos."""
from pathlib import Path

from awf.todos import is_closed, has_progress, list_active_todos


class TestIsClosed:

    def test_canonical_done(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_legacy_short_done(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (outbox / "DONE-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_canonical_blocked(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (outbox / "BLOCKED-TODO-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_legacy_short_blocked(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (outbox / "BLOCKED-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_ack_in_inbox(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (inbox / "ACK-TODO-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_ack_legacy_short(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (inbox / "ACK-0001.ready").write_text("")
        assert is_closed(inbox, outbox, "TODO-0001") is True

    def test_no_closure(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        assert is_closed(inbox, outbox, "TODO-0001") is False


class TestHasProgress:

    def test_canonical_progress_md(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "PROGRESS-TODO-0001.md").write_text("some progress\n")
        assert has_progress(outbox, "TODO-0001") is True

    def test_legacy_short_progress_md(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "PROGRESS-0042.md").write_text("some progress\n")
        assert has_progress(outbox, "TODO-0042") is True

    def test_canonical_progress_ready(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "PROGRESS-TODO-0001.ready").write_text("progress\n")
        assert has_progress(outbox, "TODO-0001") is True

    def test_legacy_short_progress_ready(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "PROGRESS-0001.ready").write_text("progress\n")
        assert has_progress(outbox, "TODO-0001") is True

    def test_empty_file(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "PROGRESS-TODO-0001.md").write_text("")
        assert has_progress(outbox, "TODO-0001") is False

    def test_missing_file(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        assert has_progress(outbox, "TODO-0001") is False


class TestListActiveTodos:

    def _create_todo(self, inbox: Path, todo_id: str, content: str = "task\n") -> None:
        (inbox / f"{todo_id}.md").write_text(content)
        (inbox / f"{todo_id}.ready").write_text("")

    def test_empty_inbox(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        assert list_active_todos(inbox, outbox) == []

    def test_no_inbox_dir(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        assert list_active_todos(inbox, outbox) == []

    def test_one_todo(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0001")
        assert list_active_todos(inbox, outbox) == ["TODO-0001"]

    def test_multiple_active_highest_first(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0001")
        self._create_todo(inbox, "TODO-0003")
        self._create_todo(inbox, "TODO-0007")
        result = list_active_todos(inbox, outbox)
        assert result == ["TODO-0007", "TODO-0003", "TODO-0001"]

    def test_numeric_sort_10_beats_9(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0009")
        self._create_todo(inbox, "TODO-0010")
        result = list_active_todos(inbox, outbox)
        assert result[0] == "TODO-0010"
        assert result[1] == "TODO-0009"

    def test_closed_todo_skipped_canonical(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0001")
        self._create_todo(inbox, "TODO-0002")
        (outbox / "DONE-TODO-0002.ready").write_text("")
        result = list_active_todos(inbox, outbox)
        assert "TODO-0002" not in result
        assert "TODO-0001" in result

    def test_closed_todo_skipped_legacy(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0001")
        self._create_todo(inbox, "TODO-0002")
        (outbox / "DONE-0002.ready").write_text("")
        result = list_active_todos(inbox, outbox)
        assert "TODO-0002" not in result

    def test_closed_by_blocked_skipped(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0001")
        self._create_todo(inbox, "TODO-0005")
        (outbox / "BLOCKED-0005.ready").write_text("")
        result = list_active_todos(inbox, outbox)
        assert "TODO-0005" not in result
        assert result == ["TODO-0001"]

    def test_empty_md_skipped(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        (inbox / "TODO-0005.md").write_text("")
        (inbox / "TODO-0005.ready").write_text("")
        self._create_todo(inbox, "TODO-0006")
        result = list_active_todos(inbox, outbox)
        assert "TODO-0005" not in result
        assert result == ["TODO-0006"]

    def test_closed_even_if_highest(self, tmp_path: Path) -> None:
        inbox = tmp_path / "inbox"
        outbox = tmp_path / "outbox"
        inbox.mkdir(); outbox.mkdir()
        self._create_todo(inbox, "TODO-0009")
        self._create_todo(inbox, "TODO-0010")
        (outbox / "DONE-TODO-0010.ready").write_text("")
        result = list_active_todos(inbox, outbox)
        assert result == ["TODO-0009"]
