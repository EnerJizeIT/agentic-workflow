"""TODO-0338 (A35): the verify prompt and the no-verdict path.

Split from ``test_verify_signal_grace.py`` (ratchet: files over 400 lines).
These tests own the rule "the signal ends the work" and the guarantee that
the grace window only arms on a verdict produced by THIS run.
"""
from __future__ import annotations

import pytest

from awf import supervisor
from awf.signal_watch import HardTimeoutError, run_subprocess_until_signal
from tests.unit._signal_fakes import (
    Clock,
    install_fake_time,
    make_project,
    silent_popen,
)


def test_verify_prompt_forbids_waiting_for_the_engine():
    """The subprocess is told that the signal ends its work."""
    prompt = supervisor.build_prompt("verify", "TODO-0001")
    assert "Signal ends your work" in prompt
    assert "Do NOT poll `awf status`" in prompt
    assert "do NOT wait for the commit" in prompt


def test_no_signal_grace_does_not_apply(tmp_path, monkeypatch):
    """No verdict on disk → the engine keeps waiting (up to the hard cap)."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    signal_file = outbox / "APPROVE-TODO-0001.ready"

    fake = silent_popen()
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    install_fake_time(monkeypatch, Clock())

    with pytest.raises(HardTimeoutError):
        run_subprocess_until_signal(
            ["fake-supervisor"], cwd=tmp_path,
            watch_paths=[signal_file],
            logs_dir=tmp_path,
            signal_exit_grace=6,
            hard_timeout=30,
        )

    assert fake.killed is True
    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "proceeding to commit" not in text
    assert "hard timeout reached" in text


def test_stale_signal_before_spawn_does_not_arm_the_grace(
    tmp_path, monkeypatch
):
    """BD-22: a leftover APPROVE from a previous run is not this verdict.

    If the snapshot check regressed, grace=0 would TERM the supervisor
    before it did any work — a silent, instant no-op stage.
    """
    signal_file = tmp_path / "APPROVE-TODO-0001.ready"
    signal_file.write_text("", encoding="utf-8")

    fake = silent_popen()
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", fake)
    install_fake_time(monkeypatch, Clock())

    with pytest.raises(HardTimeoutError):
        run_subprocess_until_signal(
            ["fake-supervisor"], cwd=tmp_path,
            watch_paths=[signal_file],
            logs_dir=tmp_path,
            signal_exit_grace=0,
            hard_timeout=30,
        )

    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "proceeding to commit" not in text
    assert "U6b: signal present" not in text


def test_only_verify_spawns_with_a_grace_window(tmp_path, monkeypatch):
    """A35 narrows the change: plan/replan keep the old natural-exit wait."""
    from subprocess import CompletedProcess

    captured: list[tuple[str, object, list[str]]] = []
    waited: list[tuple[str, str, dict]] = []

    def fake_run(cmd, cwd, **kwargs):
        kind = "verify" if any(
            str(part).endswith("supervisor-verify") for part in cmd
        ) else "plan"
        captured.append((kind, kwargs.get("signal_exit_grace"), list(cmd)))
        holder = kwargs.get("signal_holder")
        if holder is not None:
            holder["signal"] = ""
        return CompletedProcess(cmd, 0)

    # A41: a verify stage without a verdict now waits for the decision; the
    # wait itself is covered by test_run_verify_wait.py — this test owns the
    # grace window only, so the wait is stubbed out.
    def fake_wait(kind, todo_id, *args, **kwargs):
        waited.append((kind, todo_id, kwargs))
        return ""

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(supervisor, "wait_for_supervisor_signal", fake_wait)
    proj = make_project(tmp_path)
    phases = str(proj / ".agentic" / "phases" / "plan.md")
    logs = proj / ".agentic" / "logs"

    supervisor.run_supervisor_via_subprocess(
        "plan", "", proj, {"models": {"supervisor": {"model": "p/test"}}},
        phases, logs,
    )
    supervisor.run_supervisor_via_subprocess(
        "verify", "TODO-0001", proj,
        {
            "wait": {"verify_grace_seconds": 7},
            "models": {"supervisor": {"model": "p/test"}},
        }, phases, logs,
    )

    assert [(kind, grace) for kind, grace, _cmd in captured] == [
        ("plan", None),
        ("verify", 7.0),
    ]
    assert [(kind, todo) for kind, todo, _kw in waited] == [
        ("verify", "TODO-0001"),
    ]
    assert waited[0][2].get("publish_waiting") is True
    plan_prompt = captured[0][2][-1]
    verify_prompt = captured[1][2][-1]
    assert "Signal ends your work" not in plan_prompt
    assert "Signal ends your work" in verify_prompt
