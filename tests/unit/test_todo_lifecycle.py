"""Tests for DF6-1 TODO lifecycle: archive_todo, is_archived.

Covers:
- archive_todo: moves inbox/outbox/handoff files to done/{id}/
- archive_todo: idempotent (safe to call multiple times)
- archive_todo: returns None when nothing to archive
- is_archived: checks done/ directory existence
- Edge cases: missing files, partial state, REVIEW signals preserved
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.todos import archive_todo, is_archived


@pytest.fixture
def project(tmp_git_repo):
    """Project with .agentic/ structure."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _setup_todo(project: Path, todo_id: str = "TODO-0001") -> None:
    """Create a full TODO lifecycle state in inbox + outbox + handoff."""
    inbox = project / ".agentic" / "inbox"
    outbox = project / ".agentic" / "outbox"
    handoff = project / ".agentic" / "handoff"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    handoff.mkdir(parents=True, exist_ok=True)

    # Inbox files
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n")
    (inbox / f"TODO-{todo_id}.ready").write_text("")
    (inbox / f"ACK-{todo_id}.ready").write_text("")
    (inbox / f"APPROVE-{todo_id}.ready").write_text("")

    # Outbox files
    (outbox / f"DONE-{todo_id}.md").write_text("# Done\nwork complete\n")
    (outbox / f"DONE-{todo_id}.ready").write_text("")
    (outbox / f"PROGRESS-{todo_id}.md").write_text("# Progress\nstep 1 done\n")

    # Handoff files
    (handoff / f"worker-{todo_id}.md").write_text("# Worker handoff\n")
    (handoff / f"reviewer-{todo_id}.md").write_text("# Reviewer handoff\n")


class TestArchiveTodo:
    """DF6-1: archive_todo moves completed TODO files to done/."""

    def test_moves_todo_md(self, project):
        _setup_todo(project, "TODO-0001")
        result = archive_todo(project, "TODO-0001")
        assert result is not None
        assert (result / "TODO.md").is_file()

    def test_deletes_inbox_signals(self, project):
        _setup_todo(project, "TODO-0001")
        archive_todo(project, "TODO-0001")
        inbox = project / ".agentic/inbox"
        assert not (inbox / "TODO-0001.ready").exists()
        assert not (inbox / "ACK-0001.ready").exists()
        assert not (inbox / "APPROVE-TODO-0001.ready").exists()

    def test_deletes_todo_md_from_inbox(self, project):
        _setup_todo(project, "TODO-0001")
        archive_todo(project, "TODO-0001")
        assert not (project / ".agentic/inbox/TODO-0001.md").exists()

    def test_moves_outbox_done_md(self, project):
        _setup_todo(project, "TODO-0001")
        result = archive_todo(project, "TODO-0001")
        assert (result / "DONE.md").is_file()
        assert "work complete" in (result / "DONE.md").read_text()

    def test_deletes_outbox_ready_signals(self, project):
        _setup_todo(project, "TODO-0001")
        archive_todo(project, "TODO-0001")
        outbox = project / ".agentic/outbox"
        assert not (outbox / "DONE-TODO-0001.ready").exists()

    def test_moves_progress_md(self, project):
        _setup_todo(project, "TODO-0001")
        result = archive_todo(project, "TODO-0001")
        assert (result / "PROGRESS.md").is_file()

    def test_moves_handoff_files(self, project):
        _setup_todo(project, "TODO-0001")
        result = archive_todo(project, "TODO-0001")
        handoff_dest = result / "handoff"
        assert (handoff_dest / "worker-TODO-0001.md").is_file()
        assert (handoff_dest / "reviewer-TODO-0001.md").is_file()

    def test_handoff_cleaned_from_active_dir(self, project):
        _setup_todo(project, "TODO-0001")
        archive_todo(project, "TODO-0001")
        handoff = project / ".agentic/handoff"
        assert not list(handoff.glob("*-TODO-0001.md"))

    def test_idempotent(self, project):
        """Calling twice doesn't crash AND doesn't destroy the archive.

        AUD12-02: the second call used to hit ``not moved_anything`` and
        rmtree the whole ``done/TODO-0001/`` dir — history loss on a no-op.
        """
        _setup_todo(project, "TODO-0001")
        first = archive_todo(project, "TODO-0001")
        assert first is not None
        archived = sorted(p.name for p in first.rglob("*") if p.is_file())
        todo_md_content = (first / "TODO.md").read_text(encoding="utf-8")

        second = archive_todo(project, "TODO-0001")
        assert second is None  # nothing to archive second time

        for name in ("TODO.md", "DONE.md", "PROGRESS.md"):
            assert (first / name).is_file(), f"{name} destroyed by second archive call"
        for name in ("worker-TODO-0001.md", "reviewer-TODO-0001.md"):
            assert (first / "handoff" / name).is_file(), f"handoff/{name} destroyed by second archive call"
        assert sorted(p.name for p in first.rglob("*") if p.is_file()) == archived
        assert (first / "TODO.md").read_text(encoding="utf-8") == todo_md_content

    def test_returns_none_when_nothing_to_archive(self, project):
        result = archive_todo(project, "TODO-9999")
        assert result is None

    def test_partial_state_only_inbox(self, project):
        """TODO exists in inbox but no outbox files (worker didn't finish)."""
        inbox = project / ".agentic/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# Task\n")
        (inbox / "TODO-0001.ready").write_text("")
        result = archive_todo(project, "TODO-0001")
        assert result is not None
        assert (result / "TODO.md").is_file()

    def test_archives_legacy_short_id_signals(self, project):
        """AUD01-05: legacy DONE-NNNN / PROGRESS-NNNN (no TODO- prefix) must
        also move to the archive — otherwise leftovers stay in outbox."""
        inbox = project / ".agentic/inbox"
        outbox = project / ".agentic/outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# Task\n")
        (inbox / "TODO-0001.ready").write_text("")
        (outbox / "DONE-0001.md").write_text("# legacy done\n")
        (outbox / "DONE-0001.ready").write_text("")
        (outbox / "PROGRESS-0001.md").write_text("# legacy progress\n")

        result = archive_todo(project, "TODO-0001")

        assert result is not None
        assert (result / "DONE.md").is_file()
        assert "legacy done" in (result / "DONE.md").read_text()
        assert (result / "PROGRESS.md").is_file()
        assert not (outbox / "DONE-0001.md").exists()
        assert not (outbox / "DONE-0001.ready").exists()
        assert not (outbox / "PROGRESS-0001.md").exists()

    def test_preserves_review_signal(self, project):
        """REVIEW-TODO-0001.md should NOT be archived (it's a rejection)."""
        outbox = project / ".agentic/outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "REVIEW-TODO-0001.md").write_text("# Review feedback\n")
        archive_todo(project, "TODO-0001")
        # REVIEW should still be in outbox (not moved)
        assert (outbox / "REVIEW-TODO-0001.md").exists()

    def test_multiple_todos_archive_independently(self, project):
        _setup_todo(project, "TODO-0001")
        _setup_todo(project, "TODO-0002")
        r1 = archive_todo(project, "TODO-0001")
        r2 = archive_todo(project, "TODO-0002")
        assert r1 is not None
        assert r2 is not None
        assert r1 != r2


class TestIsArchived:
    """DF6-3: is_archived checks done/ directory."""

    def test_false_before_archive(self, project):
        _setup_todo(project, "TODO-0001")
        assert is_archived(project, "TODO-0001") is False

    def test_true_after_archive(self, project):
        _setup_todo(project, "TODO-0001")
        archive_todo(project, "TODO-0001")
        assert is_archived(project, "TODO-0001") is True

    def test_false_for_nonexistent_todo(self, project):
        assert is_archived(project, "TODO-9999") is False


class TestArchiveLegacyHandoffs:
    """Day-3 dashboard review: legacy agent-written handoff names get archived."""

    def test_moves_legacy_final_handoff(self, project):
        _setup_todo(project, "TODO-0001")
        handoff = project / ".agentic" / "handoff"
        (handoff / "agent-qa-review-TODO-0001-final.md").write_text("# final\n")

        result = archive_todo(project, "TODO-0001")

        assert (result / "handoff" / "agent-qa-review-TODO-0001-final.md").is_file()
        assert not (handoff / "agent-qa-review-TODO-0001-final.md").exists()

    def test_does_not_touch_other_todo(self, project):
        _setup_todo(project, "TODO-0001")
        handoff = project / ".agentic" / "handoff"
        (handoff / "agent-x-TODO-0002.md").write_text("# other\n")
        (handoff / "agent-x-TODO-00010.md").write_text("# ten\n")

        archive_todo(project, "TODO-0001")

        assert (handoff / "agent-x-TODO-0002.md").exists()
        assert (handoff / "agent-x-TODO-00010.md").exists()
