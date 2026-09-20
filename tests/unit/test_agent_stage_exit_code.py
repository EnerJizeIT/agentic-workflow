"""U6b Part A: a signal is the stage's contract, the exit code after it is noise.

TODO-0014: the worker wrote DONE (and the full report), but the opencode
process exited rc=1 (context compaction + tool timeout). The engine treated
the stage as failed and stopped the pipeline. With a valid signal present the
worker's non-zero exit code must NOT fail the stage; without a signal,
rc!=0 stays a failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import agent_stage
from awf.pipeline import Stage


def _capture_run(monkeypatch, fake_rc: int, write_signal: bool, outbox: Path):
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 **kwargs):
        captured["cmd"] = cmd
        if write_signal:
            (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
            (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        class _R:
            returncode = fake_rc

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(
        agent_stage, "collect_handoff",
        lambda *a, **kw: captured.__setitem__("handoff", True),
    )
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "inbox").mkdir(parents=True)
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    roles = proj / ".agentic" / "roles"
    roles.mkdir(parents=True)
    (roles / "developer.md").write_text("# Developer role\n", encoding="utf-8")
    return proj


def test_signal_present_nonzero_exit_is_not_a_failure(tmp_path, monkeypatch):
    """DONE written + rc=1 → stage succeeds, handoff collected, log says
    the exit code was ignored."""
    proj = _project(tmp_path)
    outbox = proj / ".agentic" / "outbox"
    logs: list[str] = []
    monkeypatch.setattr(
        agent_stage, "_log", lambda logs_dir, msg: logs.append(str(msg))
    )
    captured = _capture_run(monkeypatch, fake_rc=1, write_signal=True, outbox=outbox)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    agent_stage.run_agent_stage(
        stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs"
    )

    assert captured.get("handoff"), (
        "a signaled run must reach collect_handoff — the stage is done"
    )
    assert any(
        "signal present" in m and "ignoring worker exit code 1" in m for m in logs
    ), f"no 'signal present; ignoring worker exit code' in log: {logs}"


def test_no_signal_nonzero_exit_stays_a_failure(tmp_path, monkeypatch):
    """Regression guard: rc=1 with NO signal must still raise (the stage
    did not fulfil its contract — silent exit / crash)."""
    proj = _project(tmp_path)
    outbox = proj / ".agentic" / "outbox"
    monkeypatch.setattr(agent_stage, "_log", lambda *a, **kw: None)
    captured = _capture_run(monkeypatch, fake_rc=1, write_signal=False, outbox=outbox)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    with pytest.raises(RuntimeError, match="exited with code 1"):
        agent_stage.run_agent_stage(
            stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs"
        )
    assert "handoff" not in captured
