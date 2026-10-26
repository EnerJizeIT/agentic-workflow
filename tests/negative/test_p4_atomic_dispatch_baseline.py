"""TODO-0261 / RB-4: the dispatch baseline is a timeout-bounded smoke
snapshot with a write-ahead journal.

Four invariants:
(a) a hanging baseline command is killed at the dispatch timeout, the unit is
    still dispatched (``.ready`` exists) and the status says ``timeout``;
(b) ``no_baseline_tests`` skips the command entirely — sha/status + ``.ready``
    without running anything;
(c) a dispatch killed mid-baseline leaves a journal, and ``resume_dispatch``
    finishes the unit (md + baseline + ``.ready``) without a re-dispatch;
(d) a repeated explicit id is refused without ``force`` (old behaviour) and
    the existing TODO is never overwritten.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from awf import api
from awf.api._errors import AwfApiError
from awf.api.dispatch import dispatch_todo, resume_dispatch

TODO = "TODO-0001"


def _project(tmp_path: Path, test_cmd: str = "", baseline_timeout: str = "") -> Path:
    proj = tmp_path / "repo"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)
    api.init_project(proj, project_name="RB4", test_cmd=test_cmd or "/bin/true")
    if baseline_timeout:
        config = proj / ".agentic" / "config.yaml"
        text = config.read_text(encoding="utf-8")
        assert "verification:" in text
        text = text.replace(
            "verification:\n",
            f"verification:\n  baseline_timeout: {baseline_timeout}\n",
            1,
        )
        config.write_text(text, encoding="utf-8")
    return proj


def _ready(proj: Path, todo_id: str = TODO) -> Path:
    return proj / ".agentic" / "inbox" / f"{todo_id}.ready"


def _journal(proj: Path, todo_id: str = TODO) -> Path:
    return proj / ".agentic" / "context" / f"DISPATCH-{todo_id}.json"


class TestHangingBaselineHasItsOwnTimeout:
    def test_timeout_records_timeout_and_still_dispatches(self, tmp_path: Path):
        sleep = shutil.which("sleep") or "/bin/sleep"
        proj = _project(tmp_path, test_cmd=f"{sleep} 30", baseline_timeout="0.5")

        started = time.monotonic()
        result = dispatch_todo(proj, "# timeout probe\n\nbody.\n", todo_id=TODO)
        elapsed = time.monotonic() - started

        assert elapsed < 15, f"dispatch held by the baseline for {elapsed:.1f}s"
        assert result.baseline_test_status == "timeout"
        assert result.baseline_timeout == pytest.approx(0.5)
        assert _ready(proj).is_file()
        log = (proj / ".agentic" / "context" / "BASELINE-TODO-0001.tests.log")
        assert "timed out after 0.5s" in log.read_text(encoding="utf-8")
        assert not _journal(proj).exists()


class TestNoBaselineTestsFlag:
    def test_command_is_not_run_and_ready_is_written(self, tmp_path: Path):
        touch = shutil.which("touch") or "/usr/bin/touch"
        marker = tmp_path / "baseline-ran.marker"
        proj = _project(tmp_path, test_cmd=f"{touch} {marker}")

        result = dispatch_todo(
            proj, "# skip probe\n\nbody.\n", todo_id=TODO, no_baseline_tests=True
        )

        assert not marker.exists(), "baseline command ran despite no_baseline_tests"
        assert result.baseline_test_status == "skipped"
        assert _ready(proj).is_file()
        assert (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").is_file()
        log = proj / ".agentic" / "context" / "BASELINE-TODO-0001.tests.log"
        assert "skipped" in log.read_text(encoding="utf-8")


class TestKilledDispatchIsResumable:
    def test_journal_survives_and_resume_finishes_the_unit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        proj = _project(tmp_path)
        import awf.api.dispatch as dispatch_mod

        def _killed(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(dispatch_mod, "create_baseline", _killed)

        with pytest.raises(KeyboardInterrupt):
            dispatch_todo(proj, "# killed probe\n\nbody.\n", todo_id=TODO)

        md = proj / ".agentic" / "inbox" / f"{TODO}.md"
        assert md.is_file()
        assert not _ready(proj).exists()
        assert _journal(proj).is_file(), "no resumable journal after the kill"

        monkeypatch.setattr(
            dispatch_mod, "create_baseline", api.pipeline.create_baseline
        )
        result = resume_dispatch(proj, TODO)

        assert result.resumed is True
        assert _ready(proj).is_file()
        assert md.read_text(encoding="utf-8").strip()
        assert not _journal(proj).exists()
        assert (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").is_file()

    def test_resume_without_journal_is_refused(self, tmp_path: Path):
        proj = _project(tmp_path)
        with pytest.raises(AwfApiError, match="no pending dispatch journal"):
            resume_dispatch(proj, TODO)


class TestRepeatDispatchKeepsTheOldRefusal:
    def test_explicit_id_is_not_overwritten_without_force(self, tmp_path: Path):
        proj = _project(tmp_path)
        first = dispatch_todo(proj, "# first\n\nfirst body.\n", todo_id=TODO)
        assert first.baseline_test_status in ("passed", "no_test_cmd", "failed")

        with pytest.raises(AwfApiError, match="already exists in inbox"):
            dispatch_todo(proj, "# second\n\nsecond body.\n", todo_id=TODO)

        md = proj / ".agentic" / "inbox" / f"{TODO}.md"
        assert "first body." in md.read_text(encoding="utf-8")
        assert not _journal(proj).exists(), "a refused dispatch left a journal"

    def test_force_rewrites_the_explicit_unit(self, tmp_path: Path):
        proj = _project(tmp_path)
        dispatch_todo(proj, "# first\n\nfirst body.\n", todo_id=TODO)

        result = dispatch_todo(
            proj, "# second\n\nsecond body.\n", todo_id=TODO, force=True
        )

        md = proj / ".agentic" / "inbox" / f"{TODO}.md"
        assert "second body." in md.read_text(encoding="utf-8")
        assert _ready(proj).is_file()
        assert not _journal(proj).exists()
        assert result.todo_id == TODO


class TestLostCollisionJournalOwnership:
    """QA1 (TODO-0261): the write-ahead journal is per ATTEMPT, not per id.

    Two parallel auto dispatches used to write the same ``DISPATCH-<id>.json``
    before either reserved the id. The loser's body then replaced the
    winner's, so a winner killed before the body write resumed with the
    LOSER's TODO — exactly the ``carry_over`` / ``claim`` silent-loss class
    the design review tried to close. The journal must belong to the attempt
    that wins the id.
    """

    def test_loser_does_not_clobber_a_pending_journal(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        import awf.api.dispatch as dispatch_mod

        def _killed(*_args, **_kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(dispatch_mod, "create_baseline", _killed)
        with pytest.raises(KeyboardInterrupt):
            dispatch_todo(proj, "# WINNER\n\nwinner body.\n", todo_id=TODO)

        # Simulate the kill landing before the body write (the pre-check grep
        # can take seconds): the journal is the ONLY copy of the winner body.
        md = proj / ".agentic" / "inbox" / f"{TODO}.md"
        md.write_text("", encoding="utf-8")
        assert _journal(proj).is_file()

        monkeypatch.setattr(
            dispatch_mod, "create_baseline", api.pipeline.create_baseline
        )
        real_next = dispatch_mod._next_todo_id
        calls = {"n": 0}

        def _first_is_occupied(project_dir):
            calls["n"] += 1
            return TODO if calls["n"] == 1 else real_next(project_dir)

        monkeypatch.setattr(dispatch_mod, "_next_todo_id", _first_is_occupied)

        lost = dispatch_todo(proj, "# LOSER\n\nloser body.\n")

        assert lost.todo_id != TODO
        # The loser moved on and left the winner's journal byte-identical.
        assert _journal(proj).is_file()
        resume_dispatch(proj, TODO)
        text = md.read_text(encoding="utf-8")
        assert "winner body." in text
        assert "loser body." not in text
        assert _ready(proj).is_file()

    def test_loser_leaves_no_stray_journal_for_an_occupied_id(
        self, tmp_path, monkeypatch
    ):
        proj = _project(tmp_path)
        import awf.api.dispatch as dispatch_mod

        # An existing unit already owns TODO (its own journal is gone: e.g.
        # the owning dispatch finished between this caller's id pick and its
        # journal write).
        (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text(
            "# occupied\n\nbody.\n", encoding="utf-8"
        )

        real_next = dispatch_mod._next_todo_id
        calls = {"n": 0}

        def _first_is_occupied(project_dir):
            calls["n"] += 1
            return TODO if calls["n"] == 1 else real_next(project_dir)

        monkeypatch.setattr(dispatch_mod, "_next_todo_id", _first_is_occupied)

        result = dispatch_todo(proj, "# other\n\nother body.\n")

        assert result.todo_id != TODO
        assert not _journal(proj).exists(), (
            "a dispatch that lost the id left a journal for a unit it never owned"
        )
        assert not (proj / ".agentic" / "inbox" / f"{TODO}.ready").exists()


class TestResumeFromUnpublishedJournal:
    """The attempt-unique journal covers the pre-reservation kill too: the
    canonical file is only promoted after the id is won, so the temp is the
    only survivor — resume must still find it."""

    def test_resume_recreates_the_md_from_the_attempt_temp(self, tmp_path):
        proj = _project(tmp_path)
        import awf.api.dispatch as dispatch_mod

        tmp = dispatch_mod._write_journal(
            proj, TODO, "# pre-reservation\n\nbody.\n"
        )
        assert tmp.is_file()
        assert not dispatch_mod._journal_path(proj, TODO).exists()

        result = resume_dispatch(proj, TODO)

        assert result.resumed is True
        md = proj / ".agentic" / "inbox" / f"{TODO}.md"
        assert md.read_text(encoding="utf-8") == "# pre-reservation\n\nbody.\n"
        assert _ready(proj).is_file()
        assert not tmp.exists()
