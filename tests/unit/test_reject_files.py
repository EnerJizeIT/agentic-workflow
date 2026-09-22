"""RUN5 #1 (TODO-0052): leak-gate — files of a rejected attempt must not be
lost on retry.

Reject → retry: untracked files created by the rejected attempt stay in the
tree. The retry's baseline would record them as "pre-existing untracked" and
the commit gate (awf/commit_gate.py::_files_changed_since_baseline) would
exclude them from the retry commit. Two real incidents (FU-09 fdb3fd0, U8b
f0fea09) were fixed by manual repair commits.

Covered here:
- reject writes .agentic/context/REJECT-<todo>.files (untracked-only,
  sorted, best-effort);
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

from awf import api
from awf.reject_files import (
    orphaned_reject_files,
    read_reject_files,
    reject_files_path,
    resolve_carry_over,
    snapshot_rejected_files,
)

ORIGIN = "TODO-0001"
RETRY = "TODO-0002"


@pytest.fixture
def leak_project(tmp_git_repo: Path) -> Path:
    """Committed repo + .agentic skeleton; .agentic is gitignored."""
    for sub in ("inbox", "outbox", "context", "logs"):
        (tmp_git_repo / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
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
    """Part A.1: reject records the attempt's untracked new files."""

    def test_records_new_untracked_minus_baseline(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file(), "reject must write REJECT-TODO-0001.files"
        assert rf.read_text(encoding="utf-8").splitlines() == ["b.py", "src/a.py"]
        # stale.txt pre-existed (in BASELINE-*.untracked) — not the attempt's
        assert "stale.txt" not in rf.read_text(encoding="utf-8")

    def test_no_new_files_writes_empty_marker(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / "README.md").write_text("tracked change only\n")
        api.reject_commit(leak_project, ORIGIN, "tracked-only work")
        rf = reject_files_path(leak_project, ORIGIN)
        assert rf.is_file(), "an empty REJECT file is the explicit 'nothing new' marker"
        assert rf.read_text(encoding="utf-8").splitlines() == []

    def test_no_baseline_records_all_untracked(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        (leak_project / "a.py").write_text("x\n")
        (leak_project / "b.py").write_text("x\n")
        api.reject_commit(leak_project, ORIGIN, "no baseline snapshot")
        assert reject_files_path(leak_project, ORIGIN).read_text(
            encoding="utf-8"
        ).splitlines() == ["a.py", "b.py"]

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


class TestDispatchCarryOver:
    """Part A.2: dispatch excludes carried-over paths from baseline-untracked."""

    def test_carry_over_excludes_paths_from_baseline_untracked(
        self, leak_project: Path
    ) -> None:
        _make_rejected_attempt(leak_project)
        res = api.dispatch_todo(
            leak_project,
            "# TODO-0002\nretry with carry-over\n",
            todo_id=RETRY,
            carry_over_from=ORIGIN,
        )
        baseline_untracked = _ctx(leak_project) / f"BASELINE-{RETRY}.untracked"
        lines = baseline_untracked.read_text(encoding="utf-8").splitlines()
        assert "src/a.py" not in lines, "carried-over file must not look pre-existing"
        assert "b.py" not in lines
        assert "stale.txt" in lines, "genuinely pre-existing untracked stays listed"
        assert res.carry_over_from == ORIGIN
        assert res.carry_over_files == ["b.py", "src/a.py"]

    def test_carry_over_writes_audit_link(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        api.dispatch_todo(
            leak_project, "# TODO-0002\nretry\n", todo_id=RETRY, carry_over_from=ORIGIN
        )
        link = _ctx(leak_project) / f"BASELINE-{RETRY}.carry_over"
        assert link.is_file(), "audit trail: which origin the carry-over came from"
        assert link.read_text(encoding="utf-8").strip() == ORIGIN

    def test_without_carry_over_behavior_unchanged(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        api.dispatch_todo(leak_project, "# TODO-0002\nretry\n", todo_id=RETRY)
        lines = _ctx(leak_project) / f"BASELINE-{RETRY}.untracked"
        text = lines.read_text(encoding="utf-8").splitlines()
        assert "src/a.py" in text and "b.py" in text, "legacy path: all untracked listed"
        assert not (_ctx(leak_project) / f"BASELINE-{RETRY}.carry_over").exists()

    def test_refused_missing_reject_file_no_side_effects(
        self, leak_project: Path
    ) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        with pytest.raises(api.AwfApiError, match="REJECT-TODO-0001"):
            api.dispatch_todo(
                leak_project,
                "# TODO-0002\nretry\n",
                todo_id=RETRY,
                carry_over_from=ORIGIN,
            )
        assert not (leak_project / ".agentic" / "inbox" / f"{RETRY}.md").exists()

    def test_refused_unknown_origin_no_side_effects(self, leak_project: Path) -> None:
        with pytest.raises(api.AwfApiError, match="TODO-0099"):
            api.dispatch_todo(
                leak_project,
                "# TODO-0002\nretry\n",
                todo_id=RETRY,
                carry_over_from="TODO-0099",
            )
        assert not (leak_project / ".agentic" / "inbox" / f"{RETRY}.md").exists()


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


class TestCarryOverLinkLifecycle:
    """RUN5 #1: the BASELINE-{todo}.carry_over link describes the baseline's
    exclusion — a (re-)dispatch without carry-over rewrites the baseline
    without it, so the stale link must go (awf run_next re-applies the link
    when re-baselining)."""

    def test_non_carry_redispatch_clears_stale_link(self, leak_project: Path) -> None:
        _make_rejected_attempt(leak_project)
        api.dispatch_todo(
            leak_project,
            "# TODO-0002\nretry\n",
            todo_id=RETRY,
            carry_over_from=ORIGIN,
        )
        link = _ctx(leak_project) / f"BASELINE-{RETRY}.carry_over"
        assert link.is_file()

        # free the id, then re-dispatch WITHOUT carry-over
        (leak_project / ".agentic" / "inbox" / f"{RETRY}.md").unlink()
        api.dispatch_todo(leak_project, "# TODO-0002\nretry again\n", todo_id=RETRY)

        assert not link.exists(), (
            "non-carry re-dispatch must drop the stale carry-over link"
        )
        lines = (
            _ctx(leak_project) / f"BASELINE-{RETRY}.untracked"
        ).read_text(encoding="utf-8").splitlines()
        assert "src/a.py" in lines, "non-carry baseline lists all untracked again"
