"""Test: _next_todo_id checks done/ directory (ID collision regression).

Bug: dispatch_todo scanned only inbox + outbox for existing IDs.
When TODO-0001 was archived to done/, next dispatch created TODO-0001
again → BD-30 skipped it as 'archived' → pipeline stuck at plan.
"""
from __future__ import annotations

import subprocess

import pytest

from awf import api, paths
from awf.api.dispatch import _next_todo_id


@pytest.fixture
def project(tmp_git_repo):
    return tmp_git_repo


class TestNextTodoIdDoneCheck:
    """_next_todo_id must check done/ to avoid ID collisions."""

    def test_empty_project_returns_0001(self, project):
        result = _next_todo_id(project)
        assert result == "TODO-0001"

    def test_done_0001_returns_0002(self, project):
        done = paths.done_dir(project) / "TODO-0001"
        done.mkdir(parents=True)
        (done / "TODO.md").write_text("stub")
        result = _next_todo_id(project)
        assert result == "TODO-0002"

    def test_done_0001_and_0002_returns_0003(self, project):
        for tid in ("TODO-0001", "TODO-0002"):
            d = paths.done_dir(project) / tid
            d.mkdir(parents=True)
            (d / "TODO.md").write_text("stub")
        result = _next_todo_id(project)
        assert result == "TODO-0003"

    def test_inbox_0003_and_done_0001_returns_0004(self, project):
        """Inbox has TODO-0003, done/ has TODO-0001 → next is TODO-0004."""
        done = paths.done_dir(project) / "TODO-0001"
        done.mkdir(parents=True)
        inbox = paths.inbox(project)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0003.md").write_text("stub")
        result = _next_todo_id(project)
        assert result == "TODO-0004"

    def test_gap_in_sequence(self, project):
        """done/ has 0001 and 0003 (gap at 0002) → next is 0004."""
        for tid in ("TODO-0001", "TODO-0003"):
            d = paths.done_dir(project) / tid
            d.mkdir(parents=True)
        result = _next_todo_id(project)
        assert result == "TODO-0004"


class TestDispatchWithoutCommits:
    """AUD05-08: a git repo with zero commits is a normal pet-project start.
    dispatch_todo must raise a clean AwfApiError (not raw RuntimeError) and
    roll back the orphan TODO.md."""

    def test_raises_awf_api_error_not_runtime_error(self, project):
        api.init_project(project, project_name="NoCommits")
        subprocess.run(["git", "update-ref", "-d", "HEAD"], cwd=project, check=True)

        with pytest.raises(api.AwfApiError, match="no commits"):
            api.dispatch_todo(project, "# Task\n")

    def test_orphan_todo_md_rolled_back(self, project):
        api.init_project(project, project_name="NoCommits2")
        subprocess.run(["git", "update-ref", "-d", "HEAD"], cwd=project, check=True)

        with pytest.raises(api.AwfApiError):
            api.dispatch_todo(project, "# Task\n")

        assert not (paths.inbox(project) / "TODO-0001.md").exists()
