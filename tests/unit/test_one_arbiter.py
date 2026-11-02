"""A29 (TODO-0340): one arbiter per unit — the human outranks the auto run.

Incident 01.11 (TODO-0321): the owner approved via ``awf_approve``, the
engine then spawned its verify subprocess, that one rejected, and the
engine replanned an already accepted unit.

The contract under test:
- every human verdict names its source inside the signal;
- a human verdict present before the verify stage → no subprocess;
- a human verdict written while the subprocess runs wins, with the
  required log line, and creates no replan TODO;
- the auto-only path is unchanged;
- the priority is symmetric (auto APPROVE + human REVIEW) and does not
  resurrect a dead, unbound approval (AUD04-04/U6b stay intact).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from awf import api, arbiter, supervisor
from awf import opencode_version as ov
from tests.unit._signal_fakes import make_project

TODO = "TODO-0001"

#: The wall clock the pre-spawn freshness check sees. A fake approve carries
#: this mtime, so "issued in the second the verify stage starts" is
#: deterministic: with a live ``time.time()`` the test crossed a second
#: boundary now and then and went red for no behavioural reason.
JUST_NOW = 1_700_000_000.0


def _run_verify(proj: Path, monkeypatch) -> str:
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )
    return supervisor.run_supervisor_via_subprocess(
        "verify",
        TODO,
        proj,
        {},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )


def _write_human_approve(proj: Path, mtime: float | None = None) -> Path:
    signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    signal.write_text(json.dumps({"source": "human"}), encoding="utf-8")
    if mtime is not None:
        os.utime(signal, (mtime, mtime))
    return signal


def _write_auto_review(proj: Path) -> Path:
    review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
    review.write_text(f"# REVIEW — {TODO}\n\n## Reason\nauto\n", encoding="utf-8")
    return review


def _no_spawn(monkeypatch) -> None:
    def fake_run(*_a, **_kw):
        raise AssertionError("the verify subprocess must not be spawned")

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )


def _fake_subprocess(writes: list[Path]):
    def fake_run(cmd, cwd, **kwargs):
        for path in writes:
            path.write_text("auto verdict\n", encoding="utf-8")

        return _ReturnZero()

    return fake_run


class _ReturnZero:
    """The finished-process stand-in the fakes return."""

    returncode = 0


def _log(proj: Path) -> str:
    return (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )


def _inbox_todos(proj: Path) -> list[str]:
    return sorted(p.name for p in (proj / ".agentic" / "inbox").glob("TODO-*.md"))


class TestHumanVerdictSkipsSubprocess:
    def test_pre_existing_human_approve_does_not_spawn(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        _write_human_approve(proj, mtime=JUST_NOW)
        monkeypatch.setattr("awf.supervisor.time.time", lambda: JUST_NOW + 0.5)
        _no_spawn(monkeypatch)

        assert _run_verify(proj, monkeypatch) == f"APPROVE-{TODO}"
        assert "supervisor subprocess skipped" in _log(proj)

    def test_human_approve_survives_a_missing_role_file(self, tmp_path, monkeypatch):
        """The verify stage ABORTS on an empty supervisor signal, so a green
        owner approval must not be dropped just because the role prompt is
        absent — the human decision needs no subprocess."""
        proj = make_project(tmp_path)
        (proj / ".agentic" / "roles" / "supervisor.md").unlink()
        _write_human_approve(proj, mtime=JUST_NOW)
        monkeypatch.setattr("awf.supervisor.time.time", lambda: JUST_NOW + 0.5)
        _no_spawn(monkeypatch)

        assert _run_verify(proj, monkeypatch) == f"APPROVE-{TODO}"
        assert "supervisor subprocess skipped" in _log(proj)


class TestHumanVerdictOverridesAuto:
    def test_human_approve_after_auto_reject_wins(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        review = _write_auto_review(proj)
        inbox_before = _inbox_todos(proj)

        def fake_run(cmd, cwd, **kwargs):
            # The subprocess rejects first, the owner approves afterwards —
            # the exact 01.11 order.
            review.write_text(f"# REVIEW — {TODO}\n\nauto\n", encoding="utf-8")
            _write_human_approve(proj)

            class _R:
                returncode = 0

            return _R()

        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
        )

        assert _run_verify(proj, monkeypatch) == f"APPROVE-{TODO}"
        text = _log(proj)
        assert f"verify decision: human (APPROVE-{TODO}, mtime" in text
        assert f"auto verdict REVIEW-{TODO} ignored" in text
        # The overruled refusal cannot replan the unit later.
        assert not review.exists()
        # A conflict is not a reason to grow the queue.
        assert _inbox_todos(proj) == inbox_before


class TestAutoOnlyUnchanged:
    def test_auto_review_is_returned_without_a_human(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        review = _write_auto_review(proj)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([review]),
            raising=True,
        )

        assert _run_verify(proj, monkeypatch) == f"REVIEW-{TODO}"
        assert "auto verdict" not in _log(proj)


class TestSignalsCarryTheSource:
    def test_approve_names_the_human_source(self, tmp_git_repo):
        proj = tmp_git_repo
        api.init_project(proj, project_name="A29")
        api.approve_commit(proj, TODO)
        binding = json.loads(
            (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").read_text(
                encoding="utf-8"
            )
        )
        assert binding["source"] == "human"

    def test_bound_approve_names_the_human_source(self, tmp_git_repo, monkeypatch):
        """The incident's shape: the owner approved WITH a verified tree."""
        from awf import git_utils

        fp = "a" * 64
        monkeypatch.setattr(git_utils, "tree_fingerprint", lambda _p: fp)
        proj = tmp_git_repo
        api.init_project(proj, project_name="A29")
        api.approve_commit(proj, TODO, verified_sha=fp)
        signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        binding = json.loads(signal.read_text(encoding="utf-8"))
        assert binding["source"] == "human"
        assert arbiter.signal_source(signal) == "human"

    def test_reject_names_the_human_source(self, tmp_git_repo):
        proj = tmp_git_repo
        api.init_project(proj, project_name="A29")
        api.reject_commit(proj, TODO, "not good enough")
        text = (proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md").read_text(
            encoding="utf-8"
        )
        assert text.startswith("<!-- awf-source: human -->")
        assert "not good enough" in text


class TestReverseConflict:
    """A29 is symmetric: auto APPROVE, human REVIEW → the review stands."""

    def test_human_review_overrides_auto_approve(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        approve = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
        inbox_before = _inbox_todos(proj)

        def fake_run(cmd, cwd, **kwargs):
            # The subprocess approves (an unlabelled hand-made signal), then
            # the owner rejects.
            approve.write_text("", encoding="utf-8")
            review.write_text(
                arbiter.mark_human_review(f"# REVIEW — {TODO}\n\n## Reason\nno\n"),
                encoding="utf-8",
            )
            return _ReturnZero()

        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
        )

        assert _run_verify(proj, monkeypatch) == f"REVIEW-{TODO}"
        text = _log(proj)
        assert f"verify decision: human (REVIEW-{TODO}, mtime" in text
        assert f"auto verdict APPROVE-{TODO} ignored" in text
        # The overruled approval must not unlock a commit on a later cycle.
        assert not approve.exists()
        assert _inbox_todos(proj) == inbox_before

    def test_human_review_skips_the_subprocess(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        (proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md").write_text(
            arbiter.mark_human_review(f"# REVIEW — {TODO}\n\n## Reason\nno\n"),
            encoding="utf-8",
        )
        _no_spawn(monkeypatch)

        assert _run_verify(proj, monkeypatch) == f"REVIEW-{TODO}"
        assert "supervisor subprocess skipped" in _log(proj)


class TestDeadApprovalIsNotResurrected:
    """AUD04-04/U6b: an old, unbound human approve is a leftover, not a veto.

    The owner's file names no verified tree, so it cannot be tied to THIS
    attempt — only a same-second issue counts as "just issued". Honouring it
    would auto-commit on a dead approval, the exact hole the freshness gate
    closes.
    """

    def test_old_unbound_human_approve_is_ignored(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        marker = _write_human_approve(proj, mtime=JUST_NOW - 3600)
        monkeypatch.setattr("awf.supervisor.time.time", lambda: JUST_NOW)
        review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([review]),
            raising=True,
        )

        assert _run_verify(proj, monkeypatch) == f"REVIEW-{TODO}"
        text = _log(proj)
        assert "subprocess started" in text
        assert f"human APPROVE-{TODO} ignored" in text
        assert "predates this wait" in text
        # The owner's dead decision stays where the owner left it.
        assert marker.exists()


class TestSignalSource:
    def _signal(self, tmp_path: Path, name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_approve_marker_is_human(self, tmp_path):
        path = self._signal(tmp_path, f"APPROVE-{TODO}.ready", '{"source": "human"}')
        assert arbiter.signal_source(path) == arbiter.HUMAN

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "hand-made\n",
            '{"verified_sha": "abc"}',
            '{"source": "auto"}',
            '{"source": "human"',  # broken JSON is never a human verdict
        ],
    )
    def test_approve_without_the_marker_is_auto(self, tmp_path, text):
        path = self._signal(tmp_path, f"APPROVE-{TODO}.ready", text)
        assert arbiter.signal_source(path) == ""

    def test_review_marker_is_human(self, tmp_path):
        path = self._signal(
            tmp_path, f"REVIEW-{TODO}.md", f"{arbiter.REVIEW_MARKER}\n\n# REVIEW\n"
        )
        assert arbiter.signal_source(path) == arbiter.HUMAN

    def test_plain_review_is_auto(self, tmp_path):
        path = self._signal(tmp_path, f"REVIEW-{TODO}.md", "auto verdict\n")
        assert arbiter.signal_source(path) == ""

    def test_missing_file_is_auto(self, tmp_path):
        assert arbiter.signal_source(tmp_path / f"REVIEW-{TODO}.md") == ""

    def test_mark_human_review_is_idempotent_and_keeps_a_foreign_label(self):
        body = f"# REVIEW\n\n{arbiter.REVIEW_MARKER}\n"
        assert arbiter.mark_human_review(body) == body
        foreign = "<!-- awf-source: auto -->\n\n# REVIEW\n"
        assert arbiter.mark_human_review(foreign) == foreign
