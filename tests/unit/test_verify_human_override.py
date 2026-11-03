"""A29 (TODO-0364): the human verdict wins inside the override window.

Incident 03.11: the owner issued ``awf_approve`` for TODO-0359 while the
auto verify subprocess was still running. The subprocess wrote
``REVIEW-TODO-0359.md`` first, the engine took the refusal and replanned
a clone. The owner was seconds late — and lost.

The contract under test:
- an auto verdict is not applied at once: the engine first waits
  ``wait.human_override_seconds`` for the owner;
- an APPROVE inside the window wins, creates no replan TODO and logs the
  ``human override: …`` line;
- no human decision → the auto verdict applies when the window expires;
- an existing human decision is instant (no window sleep);
- an APPROVE whose ``verified_sha`` no longer matches the attempt does
  not override (the tree moved after verification).
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import arbiter, supervisor
from awf import opencode_version as ov
from tests.unit._signal_fakes import Clock, make_project

TODO = "TODO-0001"

#: The wall clock the freshness check sees, so "just issued" is stable.
JUST_NOW = 1_700_000_000.0


class _ReturnZero:
    returncode = 0


def _run_verify(proj: Path, monkeypatch, config: dict | None = None) -> str:
    monkeypatch.setattr(
        "awf.opencode_version.probe_version",
        lambda *a, **kw: ov.Probe("ok", (1, 19, 4)),
    )
    return supervisor.run_supervisor_via_subprocess(
        "verify",
        TODO,
        proj,
        config or {},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
    )


def _write_human_approve(proj: Path, binding: dict | None = None) -> Path:
    signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    signal.write_text(
        json.dumps(binding or {"source": "human"}), encoding="utf-8"
    )
    return signal


def _write_auto_review(proj: Path) -> Path:
    review = proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md"
    review.write_text(f"# REVIEW — {TODO}\n\nauto\n", encoding="utf-8")
    return review


def _fake_subprocess(writes: list[Path]):
    def fake_run(cmd, cwd=None, **kwargs):
        for path in writes:
            path.write_text("auto verdict\n", encoding="utf-8")
        return _ReturnZero()

    return fake_run


def _install_window(monkeypatch, on_sleep=None) -> Clock:
    """A fake monotonic clock advanced by the fake ``time.sleep``."""
    clock = Clock()

    def fake_sleep(dt: float) -> None:
        clock.t += dt
        if on_sleep is not None:
            on_sleep()

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)
    return clock


def _log(proj: Path) -> str:
    return (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )


def _inbox_todos(proj: Path) -> list[str]:
    return sorted(p.name for p in (proj / ".agentic" / "inbox").glob("TODO-*.md"))


class TestHumanWinsInsideWindow:
    def test_approve_inside_window_overrides_auto_review(
        self, tmp_path, monkeypatch
    ):
        proj = make_project(tmp_path)
        inbox_before = _inbox_todos(proj)
        written: list[bool] = []

        def on_sleep() -> None:
            if not written:
                written.append(True)
                _write_human_approve(proj)

        _install_window(monkeypatch, on_sleep)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([_write_auto_review(proj)]),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 60}}
        ) == f"APPROVE-{TODO}"
        text = _log(proj)
        assert (
            f"human override: APPROVE-{TODO} wins over auto REVIEW-{TODO} "
            "(window 60 s)" in text
        )
        assert f"verify decision: human (APPROVE-{TODO}, mtime" in text
        # The overruled refusal is gone, so a later cycle cannot replan.
        assert not (proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md").exists()
        # A conflict is not a reason to grow the queue.
        assert _inbox_todos(proj) == inbox_before

    def test_window_is_configurable_and_zero_skips_the_sleep(
        self, tmp_path, monkeypatch
    ):
        proj = make_project(tmp_path)
        clock = _install_window(monkeypatch)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([_write_auto_review(proj)]),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 0}}
        ) == f"REVIEW-{TODO}"
        assert clock.t == 1000.0


class TestAutoAppliesAfterWindow:
    def test_auto_review_applies_when_the_window_expires(
        self, tmp_path, monkeypatch
    ):
        proj = make_project(tmp_path)
        clock = _install_window(monkeypatch)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([_write_auto_review(proj)]),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 5}}
        ) == f"REVIEW-{TODO}"
        text = _log(proj)
        assert "human override" not in text
        # The wait really happened: the clock advanced by the window.
        assert clock.t >= 1005.0


class TestExistingHumanDecisionIsInstant:
    def test_pre_existing_approve_needs_no_window(self, tmp_path, monkeypatch):
        proj = make_project(tmp_path)
        _write_human_approve(proj)
        monkeypatch.setattr("awf.supervisor.time.time", lambda: JUST_NOW + 0.5)
        _install_window(monkeypatch)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("the verify subprocess must not be spawned")
            ),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 60}}
        ) == f"APPROVE-{TODO}"
        assert "supervisor subprocess skipped" in _log(proj)


class TestOwningTheVerdictEndsTheWindow:
    def test_same_verdict_as_the_auto_one_returns_at_once(
        self, tmp_path, monkeypatch
    ):
        """The owner's REVIEW has the auto signal's name: no window, and the
        overruled auto APPROVE is still swept (the leftover that would let a
        later cycle commit on a rejected tree)."""
        proj = make_project(tmp_path)
        auto_approve = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
        auto_approve.write_text("", encoding="utf-8")
        review = _write_auto_review(proj)
        human: list[bool] = []

        def on_sleep() -> None:
            if not human:
                human.append(True)
                review.write_text(
                    arbiter.mark_human_review(f"# REVIEW — {TODO}\n\nmine\n"),
                    encoding="utf-8",
                )

        clock = _install_window(monkeypatch, on_sleep)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([]),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 60}}
        ) == f"REVIEW-{TODO}"
        # The window ends at the owner's file, not at its deadline: one poll
        # tick is all it takes.
        assert clock.t < 1010.0
        assert "human override:" not in _log(proj)
        assert not auto_approve.exists()


class TestMovedTreeIsNotOverridden:
    def test_approve_with_a_foreign_verified_sha_does_not_override(
        self, tmp_path, monkeypatch
    ):
        """The owner approved an older tree: the refusal stands."""
        proj = make_project(tmp_path)
        written: list[bool] = []

        def on_sleep() -> None:
            if not written:
                written.append(True)
                _write_human_approve(
                    proj, {"source": "human", "verified_sha": "b" * 64}
                )

        _install_window(monkeypatch, on_sleep)
        monkeypatch.setattr(
            "awf.signal_watch.run_subprocess_until_signal",
            _fake_subprocess([_write_auto_review(proj)]),
            raising=True,
        )

        assert _run_verify(
            proj, monkeypatch, {"wait": {"human_override_seconds": 3}}
        ) == f"REVIEW-{TODO}"
        text = _log(proj)
        assert "human override:" not in text
        assert "does not match this attempt" in text
