"""AUD25 A-01 (TODO-0080) / R-03 (TODO-0087): the unit commit must not
touch the foreign git index.

Audit 2026-09-25, layer 4: ``_commit_specific_files`` did ``git add --
<files>`` and a plain ``git commit`` — the commit swallowed whatever else
was already staged, and the failure rollback (``git reset``) unstaged the
user's foreign staged changes too.

R-03 (audit 2026-09-25, layer 11) changed the mechanism: the gate no
longer refuses on foreign staged entries (A-01 refuse-first) — it commits
through a throwaway ``GIT_INDEX_FILE`` (``commit_gate.
_commit_via_isolated_index``), so the user's index is never opened. The
A-01 property (foreign staged can neither leak into the unit commit nor
be dropped by a failure rollback) is enforced by the isolation itself:

- foreign staged present → the commit proceeds, contains exactly the
  plan's files, the foreign entry stays staged, the rest of the index is
  untouched (the plan's files' entries are re-pointed at the new HEAD —
  R-03-F1, TODO-0093);
- a failing pre-commit hook → error outcome, no commit, the index is
  byte-identical (nothing was ever staged into it — no rollback needed);
- a fresh repo without commits → the first commit is built from the
  plan's files only; a foreign staged entry stays out of it and staged.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import commit_plan
from awf.commit_gate import _commit_via_isolated_index


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _status(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()


def _index_entries(repo: Path) -> str:
    """The user's index itself (paths + blob SHAs) — not relative to HEAD."""
    return subprocess.run(
        ["git", "ls-files", "--stage"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _plan(repo: Path, files: tuple[str, ...]) -> commit_plan.CommitPlan:
    return commit_plan.CommitPlan(
        todo_id="TODO-0080", generation=0, verified_sha="", files=files
    )


def _stage_foreign(repo: Path) -> None:
    """A file staged by somebody before awf got involved."""
    (repo / "foreign.txt").write_text("user WIP\n")
    subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)


def _unit_change(repo: Path) -> None:
    """The worker's change: a tracked file modified in the working tree."""
    (repo / "README.md").write_text("worker change\n")


class TestForeignStagedIndex:
    def test_foreign_staged_preserved_on_success(self, tmp_git_repo: Path) -> None:
        """A-01/R-03: foreign staged entries must not block the unit commit
        (refuse-first is gone) — the commit contains exactly the unit file,
        the foreign entry stays staged, the index is untouched."""
        repo = tmp_git_repo
        head_before = _head(repo)
        _stage_foreign(repo)
        _unit_change(repo)
        index_before = _index_entries(repo)

        outcome = _commit_via_isolated_index(repo, _plan(repo, ("README.md",)), "awf(implement): TODO-0080")

        assert outcome.status == "committed", (
            f"A-01/R-03: foreign staged entries must not refuse the unit commit — "
            f"{outcome.status}: {outcome.reason}"
        )
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert committed == ["README.md"], (
            "the commit must contain exactly the unit file — the foreign "
            "entry may not leak in"
        )
        assert _head(repo) != head_before, "the unit commit must appear"
        assert "A  foreign.txt" in _status(repo), (
            "the user's foreign file must stay staged exactly as before"
        )
        staged_vs_head = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert staged_vs_head == ["foreign.txt"], (
            "R-03-F1: after the unit commit the plan's files must match "
            f"the new HEAD in the real index — got {staged_vs_head}"
        )
        foreign_before = [line for line in index_before.splitlines() if "foreign.txt" in line]
        foreign_after = [line for line in _index_entries(repo).splitlines() if "foreign.txt" in line]
        assert foreign_after == foreign_before, (
            "the user's foreign staged entry must be untouched"
        )

    def test_hook_failure_preserves_user_index(self, tmp_git_repo: Path) -> None:
        repo = tmp_git_repo
        head_before = _head(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _stage_foreign(repo)
        _unit_change(repo)
        index_before = _index_entries(repo)

        outcome = _commit_via_isolated_index(repo, _plan(repo, ("README.md",)), "awf(implement): TODO-0080")

        assert outcome.status == "error", (
            f"a failing hook must produce an error outcome — {outcome.status}: {outcome.reason}"
        )
        assert _head(repo) == head_before, "the hook must prevent the commit"
        assert "A  foreign.txt" in _status(repo), (
            "A-01: a failed commit must not unstage the user's foreign file"
        )
        assert " M README.md" in _status(repo), (
            "the unit file must stay an unstaged working-tree change"
        )
        assert _index_entries(repo) == index_before, (
            "A-01: a failed commit must leave the user's index byte-identical"
        )


class TestCommitFailureUnstage:
    def test_hook_failure_clean_index_leaves_index_untouched(self, tmp_git_repo: Path) -> None:
        """With a clean index the hook fails and the failure path must not
        leave ANY staging behind — R-03 makes this trivially true: the gate
        never staged into the user's index at all (no wide reset, no
        per-file unstage)."""
        repo = tmp_git_repo
        head_before = _head(repo)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _unit_change(repo)

        outcome = _commit_via_isolated_index(repo, _plan(repo, ("README.md",)), "awf(implement): TODO-0080")

        assert outcome.status == "error"
        assert _head(repo) == head_before, "the hook must prevent the commit"
        assert _status(repo) == [" M README.md"], (
            "A-01: a failed commit must leave the unit file an unstaged "
            "working-tree change and nothing else staged"
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
        """An empty index in a fresh repo must not refuse the first commit
        (no HEAD → the isolated index starts from the plan's files only)."""
        repo = _fresh_repo(tmp_path)
        (repo / "README.md").write_text("init\n")

        outcome = _commit_via_isolated_index(repo, _plan(repo, ("README.md",)), "first")

        assert outcome.status == "committed", (
            f"an empty index must not refuse the first commit — {outcome.status}: {outcome.reason}"
        )
        out = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout
        assert "README.md" in out

    def test_first_commit_foreign_staged_stays_out_of_commit(self, tmp_path: Path) -> None:
        """R-03: a staged entry in a repo without commits stays out of the
        first commit (the isolated index starts empty — there is no HEAD
        tree to pull it from) and stays staged in the user's index. The
        pre-R-03 behavior (refuse the whole commit) is gone: isolation
        replaces refusal."""
        repo = _fresh_repo(tmp_path)
        (repo / "foreign.txt").write_text("user WIP\n")
        subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)
        (repo / "README.md").write_text("worker change\n")
        index_before = _index_entries(repo)

        outcome = _commit_via_isolated_index(repo, _plan(repo, ("README.md",)), "first")

        assert outcome.status == "committed", (
            f"a foreign staged entry must not refuse the first commit — "
            f"{outcome.status}: {outcome.reason}"
        )
        committed = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert committed == ["README.md"], (
            "the first commit must contain exactly the unit file"
        )
        assert "A  foreign.txt" in _status(repo), "the foreign file must stay staged"
        staged_vs_head = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert staged_vs_head == ["foreign.txt"], (
            "R-03-F1: after the first commit the plan's files must match "
            f"the new HEAD in the real index — got {staged_vs_head}"
        )
        foreign_before = [line for line in index_before.splitlines() if "foreign.txt" in line]
        foreign_after = [line for line in _index_entries(repo).splitlines() if "foreign.txt" in line]
        assert foreign_after == foreign_before, (
            "the user's foreign staged entry must be untouched"
        )
