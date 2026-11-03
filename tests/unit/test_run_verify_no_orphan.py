"""TODO-0354 (A41): the verdict arrives around the verify spawn / stop surfaces.

Split from ``test_run_verify_wait.py`` (ratchet: files over 400 lines).
These tests own the "no orphan" side: a verdict written after (or instead
of) the subprocess spawn is consumed, and the run's own stop surfaces (stop
flag, budget, closed run) end the wait with a reason instead of rc=1.
"""
from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from awf import api, run_state, supervisor
from awf.pipeline import Stage
from tests.unit._signal_fakes import (
    Clock,
    approve,
    log_text,
    make_project,
    # The autouse fixture is used by pytest, not by name.
    opencode_ok,  # noqa: F401
    run_verify,
)

TODO = "TODO-0001"


def test_subprocess_failure_still_waits_for_the_decision(tmp_path, monkeypatch):
    """The run-2 incident: opencode exits 1 after ~100s, approve comes after.

    The failed subprocess is not the run's verdict — the stage keeps waiting
    and consumes the approve instead of stopping with rc=1.
    """
    proj = make_project(tmp_path)
    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal",
        lambda cmd, cwd=None, **kw: CompletedProcess(cmd, 1),
        raising=True,
    )
    clock = Clock()
    ticks = {"n": 0}

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 1:
            approve(proj, TODO)

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = supervisor.run_supervisor_via_subprocess(
        "verify",
        TODO,
        proj,
        {},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
        wait_timeout=60,
    )

    assert signal == f"APPROVE-{TODO}"
    assert "A41: verify subprocess failed (exit=1)" in log_text(proj)


def test_no_role_file_still_waits_for_the_decision(tmp_path, monkeypatch):
    """A41: no subprocess spawned is not "no answer" — the approve counts."""
    proj = make_project(tmp_path)
    (proj / ".agentic" / "roles" / "supervisor.md").unlink()
    clock = Clock()
    ticks = {"n": 0}

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 1:
            approve(proj, TODO)

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = supervisor.run_supervisor_via_subprocess(
        "verify",
        TODO,
        proj,
        {},
        str(proj / ".agentic" / "phases" / "plan.md"),
        proj / ".agentic" / "logs",
        wait_timeout=30,
    )

    assert signal == f"APPROVE-{TODO}"
    assert "verify decision received while waiting (no subprocess)" in log_text(proj)


def test_stop_flag_stops_the_wait_with_a_clear_reason(tmp_path, monkeypatch):
    """A stop flag on the unit ends the wait explicitly, not as a silent rc=1."""
    proj = make_project(tmp_path)
    run_state.write_run(
        proj,
        active=True,
        queue=[TODO],
        index=0,
        current=TODO,
        stop_flags={TODO: ["phase-boundary"]},
    )

    with pytest.raises(supervisor.VerifyWaitStopped) as exc:
        run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert f"stop-list flag on {TODO}: phase-boundary" in str(exc.value)
    status = api.get_status(project_dir=proj)
    assert status.verify_waiting["reason"].startswith("stop-list flag")


def test_service_run_stop_flag_stops_the_wait(tmp_path, monkeypatch):
    """ORCH M5.2: a service run's stop surface is in its OWN file.

    The engine runs the SAME verify stage for a service run. Reading only
    the main run file would leave this wait blind to that run's stop flag
    until the stage deadline — the slot resolver the engine already uses
    for downtime accounting picks the run that owns the unit.
    """
    proj = make_project(tmp_path)
    run_state.write_run(
        proj,
        slot="service",
        active=True,
        queue=[{"todo_id": TODO, "pipeline": "role-draft-x"}],
        index=0,
        current=TODO,
        stop_flags={TODO: ["owner stop"]},
    )

    with pytest.raises(supervisor.VerifyWaitStopped) as exc:
        run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert f"stop-list flag on {TODO}: owner stop" in str(exc.value)


def test_closed_service_run_does_not_veto_the_active_main_run(
    tmp_path, monkeypatch
):
    """QA-1: a finished service run keeps ``queue``/``current`` after
    ``awf_run_service_finish`` and still owns the id in the resolver — it
    must not stop the wait for the MAIN run executing the same unit now.

    Repro: service run closed with the unit in its queue/current, the same
    unit restarted by the main run, the owner's APPROVE arrives one tick
    later. Before the fix the slot resolver handed back the dead service
    run and the wait stopped with "the run is no longer active".
    """
    proj = make_project(tmp_path)
    run_state.write_run(
        proj,
        slot="service",
        active=False,
        queue=[{"todo_id": TODO, "pipeline": "role-draft-x"}],
        index=0,
        current=TODO,
        stop_reason="finished",
    )
    run_state.write_run(
        proj,
        active=True,
        queue=[TODO],
        index=0,
        current=TODO,
    )
    # The main run is active, so a file-based approve additionally needs the
    # independent-verification evidence (AUD11-03).
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"RUN-EVIDENCE-{TODO}.md").write_text("# Evidence\n", encoding="utf-8")
    clock = Clock()
    ticks = {"n": 0}

    def fake_sleep(dt):
        clock.t += dt
        ticks["n"] += 1
        if ticks["n"] == 1:
            approve(proj, TODO)

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)

    signal = run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert signal == f"APPROVE-{TODO}", "a closed service run must not veto the wait"


def test_closed_service_run_does_not_hijack_the_verdict_of_the_main_run(
    tmp_path, monkeypatch
):
    """QA-1 on the VERDICT path: the decision goes to the run owning the unit.

    ``awf_approve``/``awf_reject`` refuse a service-run unit by queue
    membership alone (``run_slot_for_todo``). A closed service run keeps the
    unit in its queue, so the owner's verdict on the unit the MAIN run is
    executing was refused as "a service-run unit" — and the verify wait,
    fixed to wait correctly, had nothing to wait for. The refusal belongs to
    a LIVE service owner only.
    """
    proj = make_project(tmp_path)
    run_state.write_run(
        proj,
        slot="service",
        active=False,
        queue=[{"todo_id": TODO, "pipeline": "role-draft-x"}],
        index=0,
        current=TODO,
        stop_reason="finished",
    )
    run_state.write_run(proj, active=True, queue=[TODO], index=0, current=TODO)

    res = api.reject_commit(proj, TODO, reason="дизайн не тот")

    assert res.review_file, res.message
    assert "service-run unit" not in res.message
    main = run_state.read_run(proj)
    assert TODO in (main.get("rejects") or {}), (
        "the verdict of the unit the main run owns goes to the main run"
    )


def test_budget_exhausted_stops_the_wait_with_a_clear_reason(tmp_path, monkeypatch):
    """QA354: the budget is the same stop surface as the flag — one owner.

    The wait used to have its own copy of the run rules; a corrupt/exhausted
    budget now ends the wait through ``run_state.stop_reason_for_unit``, the
    call ``_run_next`` makes between units.
    """
    from datetime import datetime, timedelta, timezone

    proj = make_project(tmp_path)
    started = datetime.now(timezone.utc) - timedelta(minutes=10)
    run_state.write_run(
        proj,
        active=True,
        queue=[TODO],
        index=1,
        current=TODO,
        budget_minutes=5,
        started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    with pytest.raises(supervisor.VerifyWaitStopped) as exc:
        run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert "budget exhausted (5 min)" in str(exc.value)
    status = api.get_status(project_dir=proj)
    assert status.verify_waiting["reason"] == "budget exhausted (5 min)"


def test_closed_run_stops_the_wait_with_a_clear_reason(tmp_path, monkeypatch):
    """The owner closed the run while the stage was waiting."""
    proj = make_project(tmp_path)
    run_state.write_run(proj, active=False, queue=[TODO], index=1, current=TODO)

    with pytest.raises(supervisor.VerifyWaitStopped) as exc:
        run_verify(proj, monkeypatch, TODO, wait_timeout=60)

    assert "the run is no longer active" in str(exc.value)


def test_stale_run_file_for_another_unit_does_not_stop_the_wait(
    tmp_path, monkeypatch
):
    """A leftover run file owns ITS unit, not this verify stage.

    A stop flag in an old/foreign run must not kill a standalone verify
    that happens to run the same project; only ``current == this unit``
    gives the run a say.
    """
    proj = make_project(tmp_path)
    run_state.write_run(
        proj,
        active=True,
        queue=["TODO-0009", TODO],
        index=0,
        current="TODO-0009",
        stop_flags={TODO: ["owner"]},
    )
    # The run is active, so a file-based approve additionally needs the
    # independent-verification evidence (AUD11-03) — write what
    # awf_approve(evidence=...) would.
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"RUN-EVIDENCE-{TODO}.md").write_text("# Evidence\n", encoding="utf-8")
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

    assert signal == f"APPROVE-{TODO}", "a foreign run must not veto the decision"


def test_engine_prints_stop_for_the_wait_not_a_crash(tmp_git_repo, monkeypatch, capsys):
    """The engine's catch turns the wait stop into an explicit STOP line.

    ``VerifyWaitStopped`` subclasses RuntimeError; without the dedicated
    branch the owner would read "supervisor stage crashed" — the exact
    silent-stop complaint A41 is about.
    """
    from awf import pipeline_engine

    proj = tmp_git_repo
    api.init_project(proj, project_name="A41Stop")
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{TODO}.md").write_text("# Task\n", encoding="utf-8")
    (inbox / f"{TODO}.ready").write_text("", encoding="utf-8")

    def boom(*_a, **_kw):
        raise supervisor.VerifyWaitStopped(
            f"verify wait on {TODO} stopped: stop-list flag on {TODO}: owner"
        )

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", boom)
    monkeypatch.setattr(
        pipeline_engine, "_maybe_run_verify_pack", lambda *_a, **_kw: None
    )

    todo, delta, rc = pipeline_engine.execute_supervisor_stage(
        Stage(name="verify", role="supervisor", kind="verify"),
        TODO,
        auto=True,
        project_dir=proj,
        config={},
        logs_dir=proj / ".agentic" / "logs",
    )

    assert (todo, delta, rc) == (TODO, 0, 1)
    err = capsys.readouterr().err
    assert f"STOP: verify wait on {TODO} stopped" in err
    assert "crashed" not in err
    log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "verify: wait stopped" in log
