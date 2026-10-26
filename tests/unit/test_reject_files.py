"""RUN5 #1 (TODO-0052): leak-gate — files of a rejected attempt must not be
lost on retry.

Reject → retry: untracked files created by the rejected attempt stay in the
tree. The retry's baseline would record them as "pre-existing untracked" and
the commit gate (awf/commit_gate.py::_files_changed_since_baseline) would
exclude them from the retry commit. Two real incidents (FU-09 fdb3fd0, U8b
f0fea09) were fixed by manual repair commits.

Covered here:
- reject writes .agentic/context/REJECT-<todo>.files (the attempt's new
  untracked files AND its tracked delta, sorted, best-effort);
- dispatch_todo(carry_over_from=...) excludes those paths from the new
  baseline's untracked snapshot (so the retry commit includes them);
- validation: origin TODO + REJECT file must exist, no side effects on
  refusal;
- without carry_over_from the behavior is unchanged;
- orphan detection: REJECT-*.files paths that are still untracked AND still
  in the current baseline's untracked list would be excluded from the
  commit → the warning list.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, paths
from awf.reject_files import (
    orphaned_reject_files,
    read_reject_files,
    reject_files_path,
    resolve_carry_over,
    snapshot_rejected_files,
)

ORIGIN = "TODO-0001"
RETRY = "TODO-0002"


class TestByteFaithfulPaths:
    """R2.2 QA iter 4: the leak-gate speaks RAW path identity.

    The printed ``git ls-files`` C-quotes a non-ASCII name (``"\\321\\204..."``)
    while every transfer channel (``include_untracked`` / ``claim_from``) and the
    R2.1 model use raw bytes. The REJECT file and the carry-over/orphan readers
    now stay raw; the ``BASELINE-<todo>.untracked`` comparison goes through the
    same positional bridge ``pipeline._untracked_snapshot`` uses.
    """

    def test_snapshot_records_raw_non_ascii(self, leak_project: Path) -> None:
        finding = "файлу.txt"
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / finding).write_text("finding\n", encoding="utf-8")
        recorded = snapshot_rejected_files(
            leak_project, ORIGIN, paths.logs_dir(leak_project)
        )
        assert recorded == [finding], "the recorded path must be the RAW name"
        assert reject_files_path(leak_project, ORIGIN).read_text(
            encoding="utf-8"
        ).split("\x00")[:-1] == [finding], (
            "the file must hold the raw name (NUL-terminated), not a C-quote"
        )

    def test_carry_over_non_ascii_excluded_from_baseline(
        self, leak_project: Path
    ) -> None:
        finding = "файлу.txt"
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / finding).write_text("finding\n", encoding="utf-8")
        api.reject_commit(leak_project, ORIGIN, "rejected")
        api.dispatch_todo(
            leak_project, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=ORIGIN
        )
        snapshot = (
            _ctx(leak_project) / f"BASELINE-{RETRY}.untracked"
        ).read_text(encoding="utf-8")
        assert snapshot.strip() == "", (
            "the carried-over non-ASCII finding must be OWNED by the retry "
            f"(dropped from the baseline snapshot); snapshot={snapshot!r}"
        )

    def test_orphan_detection_is_byte_faithful_for_non_ascii(
        self, leak_project: Path
    ) -> None:
        finding = "файлу.txt"
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / finding).write_text("finding\n", encoding="utf-8")
        api.reject_commit(leak_project, ORIGIN, "rejected")
        # a retry WITHOUT carry-over records the finding as pre-existing; the
        # baseline file stores git's PRINTED (C-quoted) form
        api.dispatch_todo(leak_project, f"# {RETRY}\nretry\n", todo_id=RETRY)
        assert orphaned_reject_files(leak_project, RETRY) == [(ORIGIN, [finding])]


@pytest.fixture
def leak_project(tmp_git_repo: Path) -> Path:
    """Committed repo + .agentic skeleton; .agentic is gitignored."""
    for sub in ("inbox", "outbox", "context", "logs"):
        (tmp_git_repo / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    # REPORTS26 B2 (TODO-0156): dispatch requires config.yaml
    (tmp_git_repo / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Test\n"
    )
    (tmp_git_repo / ".gitignore").write_text(".agentic/\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "gitignore"], cwd=tmp_git_repo, check=True)
    return tmp_git_repo


def _ctx(repo: Path) -> Path:
    return repo / ".agentic" / "context"


def _make_rejected_attempt(
    repo: Path, todo_id: str = ORIGIN, new_files: tuple[str, ...] = ("src/a.py", "b.py")
) -> None:
    """A rejected attempt: origin TODO in inbox, baseline snapshot, new
    untracked files, then reject (which must write the REJECT file)."""
    (repo / ".agentic" / "inbox" / f"{todo_id}.md").write_text(f"# {todo_id}\nold work\n")
    (repo / ".agentic" / "context" / f"BASELINE-{todo_id}.untracked").write_text(
        "stale.txt\n"
    )
    (repo / "stale.txt").write_text("pre-existing untracked\n")
    for f in new_files:
        p = repo / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("attempt work\n")
    api.reject_commit(repo, todo_id, "rejected for test")


class TestSnapshotRejectedFiles:
    """Part A.1 + RB-2: reject records the attempt's untracked new files and
    its tracked delta."""

    def test_records_new_untracked_minus_baseline(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file(), "reject must write REJECT-TODO-0001.files"
        assert rf.read_text(encoding="utf-8").split("\x00")[:-1] == [
            "b.py",
            "src/a.py",
        ]
        # stale.txt pre-existed (in BASELINE-*.untracked) — not the attempt's
        assert "stale.txt" not in rf.read_text(encoding="utf-8")

    def test_tracked_change_is_recorded(self, leak_project: Path) -> None:
        """RB-2: a tracked edit of the attempt is a finding too — it must
        survive a retry, so the reject snapshot records it."""
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / "tracked.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=leak_project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "tracked base"], cwd=leak_project, check=True
        )
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        # a known baseline: empty .status = clean pre-dispatch worktree (an
        # ABSENT .status means "unknown" and suppresses the tracked delta)
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.status").write_text("")
        (leak_project / "tracked.txt").write_text("attempt edit\n", encoding="utf-8")
        api.reject_commit(leak_project, ORIGIN, "tracked-only work")
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file(), "reject must write REJECT-TODO-0001.files"
        assert rf.read_text(encoding="utf-8").split("\x00")[:-1] == ["tracked.txt"]

    def test_pre_dispatch_tracked_change_is_not_recorded(
        self, leak_project: Path
    ) -> None:
        """RB-2 boundary: only the attempt's tracked paths are recorded —
        a tracked change the baseline already carried dirty stays out."""
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / "tracked.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=leak_project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "tracked base"], cwd=leak_project, check=True
        )
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.status").write_text(
            " M tracked.txt\n"
        )
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / "tracked.txt").write_text("pre-dispatch work\n", encoding="utf-8")
        api.reject_commit(leak_project, ORIGIN, "nothing new")
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file()
        assert rf.read_text(encoding="utf-8").split("\x00")[:-1] == []

    def test_rename_records_both_sides(self, leak_project: Path) -> None:
        """RB-2: a rename owns BOTH members of the pair (the source is gone
        from git's index, not a heuristic); a copy would own only its new
        path. Recording only the destination would let the retry see a mixed
        ownership pair and refuse the whole rename."""
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / "tracked.txt").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=leak_project, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "tracked base"], cwd=leak_project, check=True
        )
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        # a known baseline: empty .status = clean pre-dispatch worktree (an
        # ABSENT .status means "unknown" and suppresses the tracked delta)
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.status").write_text("")
        subprocess.run(
            ["git", "mv", "tracked.txt", "renamed.txt"],
            cwd=leak_project,
            check=True,
        )
        api.reject_commit(leak_project, ORIGIN, "renamed work")
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file()
        assert rf.read_text(encoding="utf-8").split("\x00")[:-1] == [
            "renamed.txt",
            "tracked.txt",
        ], "both members of a rename pair must be recorded"

    def test_no_baseline_records_all_untracked(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / "a.py").write_text("x\n")
        (leak_project / "b.py").write_text("x\n")
        api.reject_commit(leak_project, ORIGIN, "no baseline snapshot")
        assert reject_files_path(leak_project, ORIGIN).read_text(
            encoding="utf-8"
        ).split("\x00")[:-1] == ["a.py", "b.py"]

    def test_not_a_git_repo_is_best_effort(self, tmp_path: Path) -> None:
        proj = tmp_path / "nogit"
        (proj / ".agentic" / "logs").mkdir(parents=True)
        (proj / "a.py").write_text("x\n")
        got = snapshot_rejected_files(proj, ORIGIN, proj / ".agentic" / "logs")
        assert got == [], "no git → nothing recorded, no exception"

    def test_reject_result_carries_recorded_files(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        # reject already ran inside _make_rejected_attempt; check the API
        # returns the same data a second call would have.
        res = api.reject_commit(leak_project, ORIGIN, "again")
        assert res.reject_files == ["b.py", "src/a.py"]
        assert (leak_project / ".agentic" / "outbox" / f"REVIEW-{ORIGIN}.md").is_file()


class TestReadAndResolve:
    """Part A.2: carry-over validation for dispatch."""

    def test_read_missing_returns_none(self, leak_project: Path) -> None:
        assert read_reject_files(leak_project, ORIGIN) is None

    def test_read_lists_paths(self, leak_project: Path) -> None:
        (reject_files_path(leak_project, ORIGIN)).write_text("b.py\nsrc/a.py\n")
        assert read_reject_files(leak_project, ORIGIN) == ["b.py", "src/a.py"]

    def test_resolve_ok(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        reject_files_path(leak_project, ORIGIN).write_text("b.py\nsrc/a.py\n")
        assert resolve_carry_over(leak_project, ORIGIN) == ["b.py", "src/a.py"]

    def test_resolve_rejects_bad_format(self, leak_project: Path) -> None:
        with pytest.raises(api.AwfApiError, match="TODO-NNNN"):
            resolve_carry_over(leak_project, "NOSUCH")

    def test_resolve_rejects_unknown_origin(self, leak_project: Path) -> None:
        reject_files_path(leak_project, "TODO-0099").write_text("b.py\n")
        with pytest.raises(api.AwfApiError, match="TODO-0099"):
            resolve_carry_over(leak_project, "TODO-0099")

    def test_resolve_rejects_missing_reject_file(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        with pytest.raises(api.AwfApiError, match="REJECT-TODO-0001"):
            resolve_carry_over(leak_project, ORIGIN)

    def test_resolve_finds_archived_origin(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "done" / ORIGIN).mkdir(parents=True)
        (leak_project / ".agentic" / "done" / ORIGIN / "TODO.md").write_text(f"# {ORIGIN}\n")
        reject_files_path(leak_project, ORIGIN).write_text("b.py\n")
        assert resolve_carry_over(leak_project, ORIGIN) == ["b.py"]


class TestOrphanDetection:
    """Part B: files that would be silently excluded from the commit."""

    def _setup(self, repo: Path, baseline_list: str) -> None:
        (repo / ".agentic" / "context" / f"REJECT-{ORIGIN}.files").write_text("src/a.py\n")
        (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked").write_text(baseline_list)
        (repo / "src").mkdir(parents=True, exist_ok=True)
        (repo / "src" / "a.py").write_text("x\n")

    def test_orphaned_when_still_in_baseline(self, leak_project: Path) -> None:
        self._setup(leak_project, "src/a.py\nstale.txt\n")
        assert orphaned_reject_files(leak_project, RETRY) == [(ORIGIN, ["src/a.py"])]

    def test_not_orphaned_after_carry_over(self, leak_project: Path) -> None:
        self._setup(leak_project, "stale.txt\n")  # src/a.py excluded at baseline
        assert orphaned_reject_files(leak_project, RETRY) == []

    def test_not_orphaned_without_baseline_untracked(self, leak_project: Path) -> None:
        self._setup(leak_project, "")
        (_ctx(leak_project) / f"BASELINE-{RETRY}.untracked").unlink()
        assert orphaned_reject_files(leak_project, RETRY) == []

    def test_not_orphaned_when_file_committed(self, leak_project: Path) -> None:
        self._setup(leak_project, "src/a.py\n")
        subprocess.run(["git", "add", "src/a.py"], cwd=leak_project, check=True)
        subprocess.run(["git", "commit", "-qm", "committed"], cwd=leak_project, check=True)
        assert orphaned_reject_files(leak_project, RETRY) == []

    def test_not_orphaned_when_file_gone(self, leak_project: Path) -> None:
        self._setup(leak_project, "src/a.py\n")
        (leak_project / "src" / "a.py").unlink()
        assert orphaned_reject_files(leak_project, RETRY) == []

    def test_multiple_origins_aggregated(self, leak_project: Path) -> None:
        self._setup(leak_project, "src/a.py\nsrc/c.py\n")
        (leak_project / ".agentic" / "context" / "REJECT-TODO-0099.files").write_text("src/c.py\n")
        (leak_project / "src" / "c.py").write_text("x\n")
        assert orphaned_reject_files(leak_project, RETRY) == [
            (ORIGIN, ["src/a.py"]),
            ("TODO-0099", ["src/c.py"]),
        ]

    def test_not_a_git_repo_returns_empty(self, tmp_path: Path) -> None:
        proj = tmp_path / "nogit"
        (proj / ".agentic" / "context").mkdir(parents=True)
        (proj / ".agentic" / "context" / f"REJECT-{ORIGIN}.files").write_text("a.py\n")
        (proj / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked").write_text("a.py\n")
        (proj / "a.py").write_text("x\n")
        assert orphaned_reject_files(proj, RETRY) == []

