"""Tests for DF6-2 _reconcile — auto state cleanup before start/continue.

Covers:
- Stale PID cleanup: state file with dead PID → cleared
- Stale PID: live PID → preserved
- ACK+APPROVE dedup: both present → ACK removed, APPROVE kept
- Multiple TODOs: WARN ONLY, never archived (NEG-2026-09-19 R2)
- Single TODO: preserved (no archival)
- No state: noop
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from awf import api
from awf.api.pipeline import _reconcile
from awf.pipeline_state import read_state, write_state
from awf.todos import is_archived


@pytest.fixture
def project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _make_todo(project: Path, todo_id: str, age_seconds: float = 0) -> None:
    """Create TODO in inbox with .md + .ready.

    todo_id should be the full ID like 'TODO-0001'.
    Files created: inbox/{todo_id}.md, inbox/{todo_id}.ready
    """
    inbox = project / ".agentic/inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    md = inbox / f"{todo_id}.md"
    ready = inbox / f"{todo_id}.ready"
    md.write_text(f"# {todo_id}\nstub\n")
    ready.write_text("")
    if age_seconds > 0:
        old_mtime = time.time() - age_seconds
        os.utime(md, (old_mtime, old_mtime))
        os.utime(ready, (old_mtime, old_mtime))


class TestReconcileStalePID:
    """DF6-2 step 1: clear stale pipeline PID."""

    def test_clears_dead_pid(self, project):
        """State file with dead PID → state cleared."""
        write_state(project, pipeline_pid="999999")  # PID 999999 doesn't exist
        _reconcile(project)
        state = read_state(project)
        assert state is None or "pipeline_pid" not in state

    def test_preserves_live_pid(self, project):
        """State file with current process PID → preserved."""
        write_state(project, pipeline_pid=str(os.getpid()))
        _reconcile(project)
        state = read_state(project)
        assert state is not None
        assert state.get("pipeline_pid") == str(os.getpid())

    def test_no_state_noop(self, project):
        """No state file → reconcile is noop. Verify inbox/outbox unchanged."""
        inbox = project / ".agentic/inbox"
        outbox = project / ".agentic/outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        _make_todo(project, "TODO-0001")
        inbox_files_before = set(p.name for p in inbox.iterdir())
        outbox_files_before = set(p.name for p in outbox.iterdir())

        _reconcile(project)

        # AUD-2026-08-09.7: verify noop contract — nothing moved/deleted
        assert read_state(project) is None
        assert set(p.name for p in inbox.iterdir()) == inbox_files_before
        assert set(p.name for p in outbox.iterdir()) == outbox_files_before


class TestReconcileDedupSignals:
    """DF6-2 step 2: deduplicate ACK+APPROVE."""

    def test_removes_ack_when_approve_exists(self, project):
        inbox = project / ".agentic/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "ACK-TODO-0001.ready").write_text("")
        (inbox / "APPROVE-TODO-0001.ready").write_text("")
        _reconcile(project)
        assert not (inbox / "ACK-TODO-0001.ready").exists()
        assert (inbox / "APPROVE-TODO-0001.ready").exists()

    def test_preserves_ack_without_approve(self, project):
        inbox = project / ".agentic/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "ACK-TODO-0001.ready").write_text("")
        _reconcile(project)
        assert (inbox / "ACK-TODO-0001.ready").exists()

    def test_no_ack_noop(self, project):
        inbox = project / ".agentic/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "APPROVE-TODO-0001.ready").write_text("")
        _reconcile(project)
        assert (inbox / "APPROVE-TODO-0001.ready").exists()


class TestReconcileMultipleTodos:
    """DF6-2 step 3 (NEG-2026-09-19 R2): multiple active TODOs are legitimate
    — a run queue pre-readies them. Reconcile must NEVER archive; it logs a
    warning and leaves everything in place."""

    def test_older_todo_left_alone(self, project):
        """Two TODOs: both stay in inbox with their ready signals."""
        _make_todo(project, "TODO-0001", age_seconds=60)
        _make_todo(project, "TODO-0002", age_seconds=0)
        _reconcile(project)
        assert is_archived(project, "TODO-0001") is False
        assert is_archived(project, "TODO-0002") is False
        inbox = project / ".agentic/inbox"
        assert (inbox / "TODO-0001.md").exists()
        assert (inbox / "TODO-0001.ready").exists()
        assert (inbox / "TODO-0002.md").exists()

    def test_single_todo_preserved(self, project):
        """One TODO → not archived."""
        _make_todo(project, "TODO-0001")
        _reconcile(project)
        assert is_archived(project, "TODO-0001") is False
        inbox = project / ".agentic/inbox"
        assert (inbox / "TODO-0001.md").exists()

    def test_three_todos_all_kept(self, project):
        _make_todo(project, "TODO-0001", age_seconds=120)
        _make_todo(project, "TODO-0002", age_seconds=60)
        _make_todo(project, "TODO-0003", age_seconds=0)
        _reconcile(project)
        for tid in ("TODO-0001", "TODO-0002", "TODO-0003"):
            assert is_archived(project, tid) is False


class TestReconcileCombined:
    """Reconcile with multiple issues at once."""

    def test_stale_pid_and_dup_signals_and_multiple_todos(self, project):
        """All three issues in one project."""
        # Stale PID
        write_state(project, pipeline_pid="999999")
        # Dup signals
        inbox = project / ".agentic/inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "ACK-TODO-0001.ready").write_text("")
        (inbox / "APPROVE-TODO-0001.ready").write_text("")
        # Multiple TODOs
        _make_todo(project, "TODO-0001", age_seconds=60)
        _make_todo(project, "TODO-0002", age_seconds=0)

        _reconcile(project)

        # PID cleared
        state = read_state(project)
        assert state is None or "pipeline_pid" not in state
        # ACK removed
        assert not (inbox / "ACK-TODO-0001.ready").exists()
        # Both TODOs stay untouched (R2: reconcile never archives)
        assert is_archived(project, "TODO-0001") is False
        assert is_archived(project, "TODO-0002") is False
