"""A29 (TODO-0364), the window's edges: config, resolution, the bound approve.

``test_verify_human_override.py`` holds the main contract (an auto verdict,
the owner's answer inside the window, the expiry, the instant pre-existing
approve). This file carries the cases the review found unpinned:

- the reverse conflict — the auto subprocess approves, the owner rejects
  (requirement 2 of the unit);
- the window's own configuration: a negative or non-numeric value must fall
  back to the 60 s default, never to "off" (a typo must not restore the
  incident);
- the incident's exact shape: ``awf_approve(verified_sha=...)`` written
  inside the window, whose binding still matches the verified tree;
- the resolution of the window: a verdict issued a second in is seen then,
  not after the deadline, and a verdict already on disk costs no sleep.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from awf import arbiter, git_utils
from awf import opencode_version as ov
from tests.unit._signal_fakes import (
    Clock,
    approve,
    install_fake_time,
    log_text,
    make_project,
    run_verify,
)

TODO = "TODO-0001"


@pytest.fixture(autouse=True)
def opencode_ok(monkeypatch):
    """Every test here spawns the supervisor: the version gate must pass."""
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )


def _window_clock(monkeypatch, on_sleep=None) -> Clock:
    """A fake monotonic clock; ``on_sleep`` writes a decision mid-window."""
    clock = Clock()
    install_fake_time(monkeypatch, clock, on_sleep)
    return clock


def _fake_subprocess_writing(files: list[tuple[Path, str]]):
    """A subprocess double that writes each ``(path, text)`` pair, verbatim."""

    def fake_run(cmd, cwd=None, **kwargs):
        for path, text in files:
            path.write_text(text, encoding="utf-8")
        return CompletedProcess(cmd, 0)

    return fake_run


def _auto_review(proj: Path) -> Path:
    """The auto subprocess's refusal — an unlabelled REVIEW in the outbox."""
    review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
    review.write_text(f"# REVIEW — {TODO}\n\nauto\n", encoding="utf-8")
    return review


class TestHumanReviewOverrulesAutoApprove:
    def test_review_inside_window_beats_auto_approve(
        self, tmp_path, monkeypatch
    ):
        """Requirement 2, the direction the incident did not take."""
        proj = make_project(tmp_path)
        auto_approve = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
        written: list[bool] = []

        def on_sleep() -> None:
            if not written:
                written.append(True)
                review.write_text(
                    arbiter.mark_human_review(f"# REVIEW — {TODO}\n\nno\n"),
                    encoding="utf-8",
                )

        _window_clock(monkeypatch, on_sleep)

        assert run_verify(
            proj, monkeypatch, TODO, {"wait": {"human_override_seconds": 60}},
            # An unlabelled file is the auto subprocess's (signal_source "").
            run=_fake_subprocess_writing([(auto_approve, "")]),
        ) == f"REVIEW-{TODO}"
        assert (
            f"human override: REVIEW-{TODO} wins over auto APPROVE-{TODO} "
            "(window 60 s)" in log_text(proj)
        )
        assert not auto_approve.exists()
        assert review.is_file()


class TestWindowConfig:
    """A typo must not switch the window off — only ``0`` does."""

    def test_negative_value_falls_back_to_the_default(self, tmp_path):
        from awf import supervisor

        logs = tmp_path / "logs"
        logs.mkdir()
        window = supervisor._human_override_seconds(
            {"wait": {"human_override_seconds": -5}}, logs
        )
        assert window == supervisor.HUMAN_OVERRIDE_DEFAULT == 60.0
        assert "is negative" in (logs / "orchestrator.log").read_text(
            encoding="utf-8"
        )

    def test_non_numeric_value_falls_back_to_the_default(self, tmp_path):
        from awf import supervisor

        logs = tmp_path / "logs"
        logs.mkdir()
        window = supervisor._human_override_seconds(
            {"wait": {"human_override_seconds": "a minute"}}, logs
        )
        assert window == 60.0
        assert "is not a number" in (logs / "orchestrator.log").read_text(
            encoding="utf-8"
        )

    def test_absent_key_is_the_default_and_zero_is_the_off_switch(
        self, tmp_path
    ):
        from awf import supervisor

        logs = tmp_path / "logs"
        logs.mkdir()
        assert supervisor._human_override_seconds({}, logs) == 60.0
        assert (
            supervisor._human_override_seconds(
                {"wait": {"human_override_seconds": 0}}, logs
            )
            == 0.0
        )


def _bound_project(tmp_path: Path) -> tuple[Path, str, str]:
    """An ``awf``-shaped git project whose APPROVE can carry a real binding.

    Returns ``(project, verified_sha, baseline_sha)`` — the numbers an
    ``awf_approve(verified_sha=...)`` on this tree publishes. The incident's
    approve was bound; a bare marker would not exercise the binding rule.
    """
    proj = tmp_path / "bound"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.t"], cwd=proj, check=True
    )
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    # The real project ignores `.agentic/`; without this the decision file
    # itself moves the fingerprint and every bound approve would look stale.
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    (proj / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)

    agentic = proj / ".agentic"
    for name in ("inbox", "outbox", "logs", "roles", "phases", "context"):
        (agentic / name).mkdir(parents=True)
    (agentic / "config.yaml").write_text(
        "stage_types:\n  verify: supervisor\n", encoding="utf-8"
    )
    (agentic / "roles" / "supervisor.md").write_text("# Role\n", encoding="utf-8")
    (agentic / "phases" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    (agentic / "context" / f"BASELINE-{TODO}.sha").write_text(
        baseline + "\n", encoding="utf-8"
    )
    verified = git_utils.tree_fingerprint(proj)
    (agentic / "context" / f"VERIFIED-{TODO}.sha").write_text(
        verified + "\n", encoding="utf-8"
    )
    return proj, verified, baseline


class TestBoundApproveInsideTheWindow:
    def test_matching_verified_sha_overrides_the_auto_review(
        self, tmp_path, monkeypatch
    ):
        """The incident's exact shape: ``awf_approve`` WITH ``verified_sha``.

        A bound approve written during the window is a statement about the
        verified tree; while that tree is still on disk it must win — the
        strict rule must not turn every bound approve into "tree moved".
        """
        proj, verified, baseline = _bound_project(tmp_path)
        written: list[bool] = []

        def on_sleep() -> None:
            if not written:
                written.append(True)
                approve(proj, TODO)
                signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
                signal.write_text(
                    json.dumps(
                        {
                            "source": "human",
                            "verified_sha": verified,
                            "baseline_sha": baseline,
                        }
                    ),
                    encoding="utf-8",
                )

        _window_clock(monkeypatch, on_sleep)
        auto = _fake_subprocess_writing([(_auto_review(proj), "# auto\n")])

        approve_signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
        assert run_verify(
            proj, monkeypatch, TODO, {"wait": {"human_override_seconds": 60}},
            run=auto,
        ) == f"APPROVE-{TODO}"
        assert "human override: APPROVE-" in log_text(proj)
        # The gate must still see the approval it acts on, and the refusal
        # must be gone, or the next cycle replans on it.
        assert approve_signal.is_file()
        assert not review.exists()


class TestWindowResolution:
    def test_a_verdict_at_second_one_is_seen_before_the_deadline(
        self, tmp_path, monkeypatch
    ):
        """Polling, not one long sleep (the incident's timing).

        With a 60 s window the owner's verdict lands in the first seconds. A
        single sleep would only look at the file after the deadline.
        """
        proj = make_project(tmp_path)
        written: list[bool] = []

        def on_sleep() -> None:
            if not written:
                written.append(True)
                approve(proj, TODO)

        clock = _window_clock(monkeypatch, on_sleep)

        assert run_verify(
            proj, monkeypatch, TODO, {"wait": {"human_override_seconds": 60}},
            run=_fake_subprocess_writing([(_auto_review(proj), "# auto\n")]),
        ) == f"APPROVE-{TODO}"
        assert clock.t - 1000.0 <= 3.0

    def test_a_verdict_written_during_the_run_costs_no_sleep(
        self, tmp_path, monkeypatch
    ):
        """Requirement 4 for the auto-verdict path: the owner decided while
        the subprocess was still running, so the first read already sees it.

        A loop that slept before its first read would hold the approval for
        the whole window.
        """
        proj = make_project(tmp_path)
        approve_signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        review = _auto_review(proj)
        clock = _window_clock(monkeypatch)

        assert run_verify(
            proj, monkeypatch, TODO, {"wait": {"human_override_seconds": 60}},
            run=_fake_subprocess_writing(
                [(review, "# REVIEW — TODO-0001\n\nauto\n"),
                 (approve_signal, json.dumps({"source": "human"}))]
            ),
        ) == f"APPROVE-{TODO}"
        assert clock.t == 1000.0
