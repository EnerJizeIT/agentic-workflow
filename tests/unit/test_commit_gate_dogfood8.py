"""Dogfood-8 regression: A1 commit isolation must include untracked files.

Discovered in ses_038a07341ffeKO2qkdB8MWCHA1: pipeline auto-commit only
included ``.gitignore`` while 35 source files stayed untracked (``??``
in git status). Root cause: ``_files_changed_since_baseline`` used
``git diff --name-only`` which shows MODIFIED tracked files only —
new files created by worker (untracked in git) were silently excluded.

Fix: combine ``git diff --name-only <sha>`` (modified) +
``git ls-files --others --exclude-standard`` (untracked).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.commit_gate import _files_changed_since_baseline


@pytest.fixture
def baseline_repo(tmp_git_repo: Path) -> tuple[Path, str]:
    """Repo with one committed baseline file. Returns (repo, baseline_sha)."""
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, text=True
    ).strip()
    return tmp_git_repo, sha


class TestFilesChangedSinceBaselineDogfood8:
    """Dogfood-8: untracked files must be included in commit isolation."""

    def test_modified_tracked_file_included(self, baseline_repo):
        """Existing file modified since baseline → in list (back-compat)."""
        repo, sha = baseline_repo
        (repo / "README.md").write_text("modified\n")

        files = _files_changed_since_baseline(repo, sha)
        assert "README.md" in files

    def test_untracked_new_file_included(self, baseline_repo):
        """Dogfood-8 regression: new file created by worker → in list.

        Before fix: ``git diff --name-only`` did not show untracked files.
        Worker created src/foo.js → silently excluded from commit.
        """
        repo, sha = baseline_repo
        (repo / "src").mkdir()
        (repo / "src" / "foo.js").write_text("console.log('hi');\n")

        files = _files_changed_since_baseline(repo, sha)
        assert "src/foo.js" in files, (
            "Dogfood-8: untracked files must be included — was silently "
            "excluded by git diff --name-only"
        )

    def test_gitignored_files_excluded(self, baseline_repo):
        """Runtime dirs (.agentic/inbox/ etc.) respect .gitignore."""
        repo, sha = baseline_repo
        # Must have .gitignore entry — without it, git ls-files includes everything.
        (repo / ".gitignore").write_text(".agentic/inbox/\n")
        (repo / ".agentic" / "inbox").mkdir(parents=True)
        (repo / ".agentic" / "inbox" / "TODO-0001.ready").write_text("")

        files = _files_changed_since_baseline(repo, sha)
        # .gitignore itself is new (untracked) — must be in list.
        assert ".gitignore" in files
        # But .agentic/inbox/ contents must NOT be in list (gitignored).
        assert not any(".agentic/inbox" in f for f in files), (
            "gitignored files must NOT be included"
        )

    def test_both_modified_and_untracked_combined(self, baseline_repo):
        """Worker modified 1 file + created 2 new → all 3 in list."""
        repo, sha = baseline_repo
        (repo / "README.md").write_text("modified\n")  # modified
        (repo / "new1.js").write_text("// new1\n")  # new
        (repo / "new2.js").write_text("// new2\n")  # new

        files = _files_changed_since_baseline(repo, sha)
        assert "README.md" in files
        assert "new1.js" in files
        assert "new2.js" in files

    def test_no_changes_returns_empty(self, baseline_repo):
        """No changes → empty list."""
        repo, sha = baseline_repo
        files = _files_changed_since_baseline(repo, sha)
        assert files == []

    def test_empty_baseline_returns_empty(self, baseline_repo):
        """Empty baseline_sha → empty list (no crash)."""
        repo, _sha = baseline_repo
        files = _files_changed_since_baseline(repo, "")
        assert files == []

    def test_deleted_file_included(self, baseline_repo):
        """File deleted since baseline → in list (git diff shows it)."""
        repo, sha = baseline_repo
        (repo / "README.md").unlink()

        files = _files_changed_since_baseline(repo, sha)
        assert "README.md" in files
