"""TODO-0354 (A41): in a run the verify stage waits for the decision.

Before this unit the auto verify stage returned "" as soon as the supervisor
subprocess exited without a verdict — the engine exited rc=1, and the
``awf_approve`` that arrived a moment later was orphaned. Now the stage
waits exactly like the interactive one: a fresh APPROVE/REVIEW is accepted,
the wait is visible as ``verify_waiting`` in ``awf status``, and the run's
own stop surfaces (stop flag, budget, closed run) end it with a reason.
"""
from __future__ import annotations

import os
import time

import pytest

from awf import api, supervisor
from tests.unit._signal_fakes import (
    Clock,
    approve,
    install_fake_time,
    log_text,
    make_project,
    # The autouse fixture is used by pytest, not by name.
    opencode_ok,  # noqa: F401
    run_verify,
)

TODO = "TODO-0001"


def test_decision_arriving_during_the_wait_ends_it(tmp_path, monkeypatch):
    """The incident: the subprocess left no verdict; the approve comes later."""
    proj = make_project(tmp_path)
    clock = Clock()
    ticks = {"n": 0}

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 2:
            approve(proj, TODO)

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert signal == f"APPROVE-{TODO}"
    text = log_text(proj)
    assert "A41: verify subprocess produced no decision" in text
    assert "A41: verify decision received while waiting" in text


def test_wait_is_anchored_to_the_spawn_not_to_the_wait_call(
    tmp_path, monkeypatch
):
    """A verdict written after the spawn but before the wait still counts."""
    proj = make_project(tmp_path)
    now = time.time()
    sig = approve(proj, TODO)
    os.utime(sig, (now - 2, now - 2))

    install_fake_time(monkeypatch, Clock())

    signal = supervisor.wait_for_supervisor_signal(
        "verify",
        TODO,
        proj,
        proj / ".agentic" / "logs",
        timeout=12,
        wait_started=now - 3,
        publish_waiting=True,
    )

    assert signal == f"APPROVE-{TODO}"


def test_no_decision_keeps_waiting_and_status_shows_verify_waiting(
    tmp_path, monkeypatch
):
    """No approve → the run stays in the wait; rc=1 only at the deadline."""
    proj = make_project(tmp_path)
    clock = Clock(5000.0)
    seen: dict = {}

    def fake_sleep(dt):
        clock.t += dt
        if "vw" not in seen:
            seen["vw"] = api.get_status(project_dir=proj).verify_waiting

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    with pytest.raises(TimeoutError):
        run_verify(proj, monkeypatch, TODO, wait_timeout=12)

    assert seen["vw"]["todo_id"] == TODO
    assert seen["vw"]["deadline_seconds"] == 12

    status = api.get_status(project_dir=proj)
    assert status.verify_waiting["reason"] == "deadline"
    assert "A41: verify_waiting — no decision within 12s" in log_text(proj)


def test_status_elapsed_is_derived_from_since_not_from_the_last_tick(
    tmp_path, monkeypatch
):
    """AR-4: the age shown in status is read from ``since``, not stored.

    The event is refreshed on the 60s log tick; between ticks the stored
    ``elapsed_seconds`` lags. A stale stored value must not reach the owner:
    the fact is ``since``, the age is derived at read time.
    """
    from datetime import datetime, timedelta, timezone

    from awf.pipeline_state import write_state

    proj = make_project(tmp_path)
    since = datetime.now(timezone.utc) - timedelta(seconds=120)
    write_state(
        proj,
        verify_waiting={
            "todo_id": TODO,
            "since": since.isoformat(),
            "elapsed_seconds": 0,
            "deadline_seconds": 600,
            "reason": "waiting",
        },
    )

    status = api.get_status(project_dir=proj)

    assert status.verify_waiting["elapsed_seconds"] >= 120


def test_status_hides_the_wait_when_the_engine_is_dead(tmp_path, monkeypatch):
    """A41: a leftover wait event must not send the owner to approve into the
    void — a dead engine owns the stage line, not the wait line."""
    proj = make_project(tmp_path)
    supervisor._publish_verify_waiting(proj, TODO, time.time(), 60, "waiting")

    from awf import _engine_liveness

    dead = _engine_liveness.EngineDead(
        stage="agent-qa-review", todo_id=TODO, reason="pid 999999 dead"
    )
    monkeypatch.setattr(
        _engine_liveness, "status_engine_dead", lambda *a, **kw: dead
    )

    status = api.get_status(project_dir=proj)

    assert status.engine_dead is not None
    assert status.verify_waiting is None


def test_wait_refreshes_the_log_and_status_every_minute(tmp_path, monkeypatch):
    """TODO item 2: a silent hour-long wait must say so, not look frozen.

    The event is published at the start and refreshed on the 60s tick; the
    owner reads "no decision yet" with the remaining deadline instead of
    guessing whether the engine is alive.
    """
    proj = make_project(tmp_path)
    clock = Clock()
    ticks = {"n": 0}
    seen: list = []

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 25:  # 75s into a 180s deadline — past one tick
            seen.append(api.get_status(project_dir=proj).verify_waiting)
            approve(proj, TODO)

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = run_verify(proj, monkeypatch, TODO, wait_timeout=180)

    assert signal == f"APPROVE-{TODO}"
    assert seen and seen[0]["todo_id"] == TODO
    assert seen[0]["deadline_seconds"] == 180
    assert (
        "A41: verify_waiting for TODO-0001 — no decision yet"
        in log_text(proj)
    )


def test_review_during_the_wait_ends_it_and_clears_the_event(
    tmp_path, monkeypatch
):
    """REVIEW is the other verdict the wait accepts — and it clears the event.

    A rejection needs no run evidence (AUD11-03 gates approves only), so a
    REVIEW written mid-wait must end the wait immediately.
    """
    proj = make_project(tmp_path)
    clock = Clock()
    ticks = {"n": 0}

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 2:
            (proj / ".agentic" / "outbox" / f"REVIEW-{TODO}.md").write_text(
                "# Review\nMissing evidence\n", encoding="utf-8"
            )

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert signal == f"REVIEW-{TODO}"
    assert api.get_status(project_dir=proj).verify_waiting is None, (
        "the event must not outlive the decision"
    )
