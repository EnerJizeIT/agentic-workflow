"""RB-2 (TODO-0259): carry-over of the rejected attempt's tracked delta.

Split out of ``test_reject_files.py`` (audit 0259, ratchet): the module grew
past 400 lines. The fixtures/helpers stay in one place — they are imported
from the sibling module, so pytest registers them here too.

Covers:
- dispatch_todo(carry_over_from=...) drops the carried-over paths from the new
  baseline snapshot (the retry commit then owns them);
- the carry-over audit link lifecycle;
- CTX1 (audit 0259): an UNKNOWN baseline (no ``BASELINE-<todo>.status``) must
  not record foreign pre-dispatch tracked changes as the attempt's delta.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, commit_plan
from awf.reject_files import read_reject_files, reject_files_path

from .test_reject_files import (  # fixtures + helpers, single source
    ORIGIN,
    RETRY,
    _ctx,
    _make_rejected_attempt,
    leak_project,
)

__all__ = ["leak_project"]


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
        from awf.infra.git_model import read_untracked_snapshot

        lines = read_untracked_snapshot(leak_project, RETRY) or set()
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
        from awf.infra.git_model import read_untracked_snapshot

        text = read_untracked_snapshot(leak_project, RETRY) or set()
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
        from awf.infra.git_model import read_untracked_snapshot

        lines = read_untracked_snapshot(leak_project, RETRY) or set()
        assert "src/a.py" in lines, "non-carry baseline lists all untracked again"


def _commit(repo: Path, name: str, content: str, message: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)


class TestUnknownBaselineTrackedDelta:
    """CTX1 (audit 0259): a missing ``BASELINE-<todo>.status`` means the
    pre-dispatch dirty set is UNKNOWN, not empty. Recording the current
    tracked changes as the attempt's delta would hand a foreign edit to the
    retry commit."""

    def test_foreign_tracked_change_not_recorded(self, leak_project: Path) -> None:
        (leak_project / ".agentic" / "inbox" / f"{ORIGIN}.md").write_text(f"# {ORIGIN}\n")
        _commit(leak_project, "a.txt", "v1\n", "a base")
        _commit(leak_project, "b.txt", "v1\n", "b base")
        # legacy baseline: an untracked snapshot, but NO .status
        (leak_project / ".agentic" / "context" / f"BASELINE-{ORIGIN}.untracked").write_text("")
        (leak_project / "a.txt").write_text("foreign pre-dispatch work\n", encoding="utf-8")
        (leak_project / "b.txt").write_text("the attempt's edit\n", encoding="utf-8")

        api.reject_commit(leak_project, ORIGIN, "unknown baseline")

        recorded = reject_files_path(leak_project, ORIGIN).read_text(
            encoding="utf-8"
        ).split("\x00")[:-1]
        assert "a.txt" not in recorded, (
            "a foreign pre-dispatch tracked edit must not become the attempt's "
            f"delta; recorded={recorded!r}"
        )

        api.dispatch_todo(
            leak_project, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=ORIGIN
        )
        baseline = commit_plan.read_baseline_sha(leak_project, RETRY)
        plan = commit_plan._plan_split(leak_project, baseline, RETRY)
        assert "a.txt" not in set(plan.files), (
            "the retry must not commit the foreign tracked edit; "
            f"files={plan.files!r}"
        )


class TestStagedPreexistingUntrackedIsForeign:
    """QA (TODO-0259): the baseline's UNTRACKED snapshot is the second foreign
    source, exactly as in ``DeltaAssembler._path_owned``. A pre-existing
    untracked file the attempt merely staged is NOT its tracked delta:
    recording it would let ``carry_over_from`` adopt it, and the retry would
    commit a file the user never handed to the unit (bypassing the
    baseline-staged protection the retry baseline otherwise holds)."""

    def test_staged_preexisting_untracked_is_not_recorded(
        self, leak_project: Path
    ) -> None:
        _commit(leak_project, "module.py", "v1\n", "module base")
        (leak_project / "user_notes.txt").write_text(
            "the user's pre-dispatch WIP\n", encoding="utf-8"
        )
        api.dispatch_todo(leak_project, f"# {ORIGIN}\norigin\n", todo_id=ORIGIN)

        (leak_project / "module.py").write_text("attempt\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "user_notes.txt"], cwd=leak_project, check=True
        )
        api.reject_commit(leak_project, ORIGIN, "rejected for the test")

        recorded = reject_files_path(leak_project, ORIGIN).read_text(
            encoding="utf-8"
        ).split("\x00")[:-1]
        assert "module.py" in recorded, recorded
        assert "user_notes.txt" not in recorded, (
            "a staged pre-existing untracked file is foreign, not the attempt's "
            f"delta; recorded={recorded!r}"
        )
        assert read_reject_files(leak_project, ORIGIN) == ["module.py"]

        api.dispatch_todo(
            leak_project,
            f"# {RETRY}\nretry\n",
            todo_id=RETRY,
            carry_over_from=ORIGIN,
        )
        baseline = commit_plan.read_baseline_sha(leak_project, RETRY)
        plan = commit_plan._plan_split(leak_project, baseline, RETRY)
        assert "module.py" in set(plan.files), plan.files
        assert "user_notes.txt" not in set(plan.files), (
            "the carry-over retry must not commit the user's pre-existing file: "
            f"files={plan.files!r}, excluded={plan.excluded!r}"
        )
