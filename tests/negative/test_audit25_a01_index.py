"""AUD25 A-01 (TODO-0080): the unit commit must not touch foreign git index.

Audit 2026-09-25, layer 4: ``_commit_specific_files`` did ``git add -- <files>``
and a plain ``git commit`` — the commit swallowed whatever else was already
staged, and the failure rollback (``git reset``) unstaged the user's foreign
staged changes too.

Safe behavior asserted here (refuse-first):
- staged entries present before the call → no commit, clear refusal, the
  index is left exactly as found (foreign stays staged, unit files stay
  unstaged in the working tree);
- a failing pre-commit hook cannot unstage the user's foreign entries —
  with refuse-first the hook failure is unreachable in that state, and in
  the clean-index state only the gate's own files get unstaged on failure.

On the pre-fix code both tests are RED: test 1 sees the commit succeed with
the foreign file inside it, test 2 sees the wide ``git reset`` unstage the
foreign file.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf.commit_gate import _commit_specific_files


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _status(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()


def _stage_foreign(repo: Path) -> None:
    """A file staged by somebody before awf got involved."""
    (repo / "foreign.txt").write_text("user WIP\n")
    subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)


def _unit_change(repo: Path) -> None:
    """The worker's change: a tracked file modified in the working tree."""
    (repo / "README.md").write_text("worker change\n")


class TestForeignStagedIndex:
    def test_foreign_staged_blocks_commit_and_preserves_index(self, tmp_git_repo: Path) -> None:
        repo = tmp_git_repo
        head_before = _head(repo)
        _stage_foreign(repo)
        _unit_change(repo)

        ok = _commit_specific_files(repo, ["README.md"], "awf(implement): TODO-0080")

        assert ok is False, "A-01: foreign staged entries must refuse the unit commit"
        assert _head(repo) == head_before, "no new commit must appear"
        status = _status(repo)
        assert "A  foreign.txt" in status, (
            "the user's foreign file must stay staged exactly as before"
        )
        assert " M README.md" in status, (
            "the unit file must stay an unstaged working-tree change"
        )

    def test_hook_failure_preserves_user_index(self, tmp_git_repo: Path) -> None:
        repo = tmp_git_repo
        head_before = _head(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _stage_foreign(repo)
        _unit_change(repo)

        ok = _commit_specific_files(repo, ["README.md"], "awf(implement): TODO-0080")

        assert ok is False
        assert _head(repo) == head_before, "the hook must prevent the commit"
        status = _status(repo)
        assert "A  foreign.txt" in status, (
            "A-01: a failed commit must not unstage the user's foreign file"
        )
        assert " M README.md" in status, (
            "the unit file must stay an unstaged working-tree change"
        )


class TestCommitFailureUnstage:
    def test_hook_failure_clean_index_unstages_only_unit_files(self, tmp_git_repo: Path) -> None:
        """With a clean index the gate stages, the hook fails, and the
        failure path must undo exactly the gate's own staging (no wide
        reset). Guards the `_unstage_unit_files` call on the rc != 0 branch."""
        repo = tmp_git_repo
        head_before = _head(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _unit_change(repo)

        ok = _commit_specific_files(repo, ["README.md"], "awf(implement): TODO-0080")

        assert ok is False
        assert _head(repo) == head_before, "the hook must prevent the commit"
        assert _status(repo) == [" M README.md"], (
            "A-01: a failed commit must undo only the gate's own staging — "
            "README back to an unstaged working-tree change, nothing else staged"
        )


def _fresh_repo(tmp_path: Path) -> Path:
    """git init + identity, NO commit (first-commit case)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    return repo


class TestFirstCommitIndex:
    def test_first_commit_clean_index_succeeds(self, tmp_path: Path) -> None:
        """An empty index in a fresh repo must NOT refuse the first commit
        (guards the `ls-files --stage` branch from over-refusing)."""
        repo = _fresh_repo(tmp_path)
        (repo / "README.md").write_text("init\n")

        ok = _commit_specific_files(repo, ["README.md"], "first")

        assert ok is True, "A-01: an empty index must not refuse the first commit"
        out = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "README.md" in out

    def test_first_commit_foreign_staged_refused(self, tmp_path: Path) -> None:
        """A staged entry in a repo without commits is foreign too — the
        gate must refuse before creating a first commit with it inside."""
        repo = _fresh_repo(tmp_path)
        (repo / "foreign.txt").write_text("user WIP\n")
        subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)
        (repo / "README.md").write_text("worker change\n")

        ok = _commit_specific_files(repo, ["README.md"], "first")

        assert ok is False, "A-01: a staged entry in a fresh repo must refuse the commit"
        assert subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=repo, capture_output=True
        ).returncode != 0, "no commit must appear"
        assert "A  foreign.txt" in _status(repo), "the foreign file must stay staged"
