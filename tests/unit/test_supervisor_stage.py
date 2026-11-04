"""A13 (TODO-0313): both spawns build ``--agent`` from one helper.

The worker (``agent_stage``) and the supervisor subprocess
(``supervisor``) must agree: an unnamed role gets no ``--agent`` at all,
a named one gets exactly one flag.
"""
from __future__ import annotations

import pytest

from awf import agent_stage, supervisor
from awf import opencode_version as ov
from awf.pipeline import Stage


def _fake_run(captured: dict, holder_signal: str = ""):
    def fake_run(cmd, cwd, **kwargs):
        captured["cmd"] = cmd
        holder = kwargs.get("signal_holder")
        if holder is not None:
            holder["signal"] = holder_signal

        class _R:
            returncode = 0

        return _R()

    return fake_run


def _project(tmp_path, role: str = "supervisor"):
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / f"{role}.md").write_text("# Role\n")
    (proj / ".agentic" / "roles" / "developer.md").write_text("# Dev\n")
    phases = proj / ".agentic" / "phases"
    phases.mkdir()
    (phases / "plan.md").write_text("# Plan\n")
    return proj


@pytest.fixture(autouse=True)
def _one_x(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (1, 19, 4))
    )


def _with_model(config: dict) -> dict:
    """A48: every spawn needs an explicit model — these tests own --agent."""
    models = {"worker": {"model": "p/test"}}
    models.update(config.get("models") or {})
    return {**config, "models": models}


def _worker_argv(tmp_path, monkeypatch, config: dict) -> list[str]:
    captured: dict = {}
    proj = _project(tmp_path)
    config = _with_model(config)
    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal",
        _fake_run(captured), raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    agent_stage.run_agent_stage(
        Stage(name="agent-dev", role="developer", kind="execute"),
        "TODO-0001", proj, config, proj / ".agentic" / "logs",
    )
    return captured["cmd"]


def _supervisor_argv(
    tmp_path, monkeypatch, config: dict, kind: str = "plan", todo_id: str = ""
) -> list[str]:
    captured: dict = {}
    proj = _project(tmp_path)
    config = _with_model(config)
    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal",
        _fake_run(captured), raising=True,
    )
    if kind == "verify":
        # A41: a verdict-less verify stage now waits for the decision; these
        # tests own the argv only (the wait has its own file).
        monkeypatch.setattr(
            supervisor, "wait_for_supervisor_signal", lambda *a, **kw: ""
        )
    phases = proj / ".agentic" / "phases" / "plan.md"
    supervisor.run_supervisor_via_subprocess(
        kind, todo_id, proj, config, str(phases), proj / ".agentic" / "logs"
    )
    return captured["cmd"]


def test_worker_spawn_without_agent_name_has_no_flag(tmp_path, monkeypatch):
    cmd = _worker_argv(tmp_path, monkeypatch, {})
    assert "--agent" not in cmd
    assert cmd[:2] == ["opencode", "run"]


def test_worker_spawn_with_agent_name_has_one_flag(tmp_path, monkeypatch):
    config = {"models": {"developer": {"agent_name": "worker"}}}
    cmd = _worker_argv(tmp_path, monkeypatch, config)
    assert cmd.count("--agent") == 1
    idx = cmd.index("--agent")
    assert cmd[idx + 1] == "worker"


def test_supervisor_spawn_without_agent_name_has_no_flag(tmp_path, monkeypatch):
    """A13 + R2: no agent flag when the role is unnamed, isolation always."""
    cmd = _supervisor_argv(tmp_path, monkeypatch, {})
    assert "--agent" not in cmd
    assert cmd[:4] == ["opencode", "run", "--standalone", "--auto"]


def test_supervisor_spawn_with_agent_name_has_one_flag(tmp_path, monkeypatch):
    config = {"models": {"supervisor": {"agent_name": "supervisor"}}}
    cmd = _supervisor_argv(tmp_path, monkeypatch, config)
    assert cmd.count("--agent") == 1
    idx = cmd.index("--agent")
    assert cmd[idx + 1] == "supervisor"


def test_verify_spawn_without_agent_name_has_no_flag(tmp_path, monkeypatch):
    """The reported crash site: ``verify`` in auto mode (A13, TODO-0313)."""
    cmd = _supervisor_argv(
        tmp_path, monkeypatch, {}, kind="verify", todo_id="TODO-0001"
    )
    assert "--agent" not in cmd
    assert cmd[:4] == ["opencode", "run", "--standalone", "--auto"]
    assert "awf-supervisor-verify" in cmd


def test_verify_spawn_with_agent_name_has_one_flag(tmp_path, monkeypatch):
    config = {"models": {"supervisor": {"agent_name": "supervisor"}}}
    cmd = _supervisor_argv(
        tmp_path, monkeypatch, config, kind="verify", todo_id="TODO-0001"
    )
    assert cmd.count("--agent") == 1


def test_worker_v2_standalone_before_auto_and_named_agent(tmp_path, monkeypatch):
    """K2 + A13 together: ``--standalone`` stays right after ``run``."""
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    config = {"models": {"developer": {"agent_name": "worker"}}}
    cmd = _worker_argv(tmp_path, monkeypatch, config)
    assert cmd[:4] == ["opencode", "run", "--standalone", "--auto"]
    assert cmd[4:7] == ["--print-logs", "--agent", "worker"]


def test_supervisor_v2_standalone_before_auto_and_named_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    config = {"models": {"supervisor": {"agent_name": "supervisor"}}}
    cmd = _supervisor_argv(tmp_path, monkeypatch, config)
    assert cmd[:4] == ["opencode", "run", "--standalone", "--auto"]
    assert cmd[4:6] == ["--agent", "supervisor"]
