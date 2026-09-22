"""RUN5 #1 (TODO-0052): commit-gate view of the leak-gate.

The gate itself (awf/commit_gate.py::_files_changed_since_baseline) is
unchanged: it commits untracked files that are NOT listed in
BASELINE-<todo>.untracked. The leak-gate works by removing the carried-over
paths from that list at baseline time (awf/api/dispatch.py, Part A.2) — so
these tests prove the end-to-end consequence:

- a carried-over file (absent from the retry's baseline-untracked) lands in
  the commit;
- a genuinely pre-existing untracked file (listed there) is still excluded;
- ``maybe_commit`` commits exactly that split.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf.commit_gate import _files_changed_since_baseline, maybe_commit

TODO = "TODO-0002"


@pytest.fixture
def gate_repo(tmp_git_repo: Path) -> tuple[Path, str]:
    """Committed repo + BASELINE-{TODO}.untracked snapshot (empty gitignore
    so .agentic/ does not pollute the untracked list)."""
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_git_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_git_repo, sha


class TestCarriedOverFiles:
    def test_carried_over_file_included(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        # The retry's baseline did NOT list src/a.py (carry-over excluded it).
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert "src/a.py" in files, (
            "RUN5 #1: the retry must commit the file the rejected attempt left behind"
        )
        assert "stale.txt" not in files, (
            "genuinely pre-existing untracked must stay out of the unit commit"
        )

    def test_pre_existing_only_is_still_excluded(self, gate_repo: tuple[Path, str]) -> None:
        repo, sha = gate_repo
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "stale.txt").write_text("pre-existing\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert files == []

    def test_without_baseline_snapshot_all_untracked_included(
        self, gate_repo: tuple[Path, str]
    ) -> None:
        """Legacy path (no BASELINE-*.untracked at all): gate includes every
        untracked file — a carry-over file is committed there too."""
        repo, sha = gate_repo
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")

        files = _files_changed_since_baseline(repo, sha, todo_id=TODO)
        assert "src/a.py" in files

    def test_maybe_commit_commits_carried_over_not_pre_existing(
        self, gate_repo: tuple[Path, str]
    ) -> None:
        repo, sha = gate_repo
        (repo / ".agentic" / "context" / f"BASELINE-{TODO}.untracked").write_text("stale.txt\n")
        (repo / "src").mkdir()
        (repo / "src" / "a.py").write_text("carried over\n")
        (repo / "stale.txt").write_text("pre-existing\n")
        logs = repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        ok = maybe_commit("implement", TODO, "commit_and_report", repo, logs,
                          auto=False, baseline_sha=sha)
        assert ok is True
        committed = subprocess.run(
            ["git", "show", "--name-only", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        assert "src/a.py" in committed
        assert "stale.txt" not in committed
        # stale.txt stays in the tree, untouched
        assert (repo / "stale.txt").is_file()
        assert subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=repo, capture_output=True, text=True, check=True,
        ).stdout.splitlines() == ["stale.txt"]
