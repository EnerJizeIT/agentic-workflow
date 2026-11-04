"""QA for TODO-0359 (A44): bugs the implementer's tests do not reach.

Three gaps this file closes:

1. The stage wiring was never exercised — the unit's tests call
   ``reap_worker_tree`` directly, so deleting the ``finally`` reap from
   ``agent_stage`` keeps every test green while the defect returns.
2. The reap was a single snapshot: a member that ignores SIGTERM and keeps
   forking during the grace window was never in the member list, so its
   children leaked — the pile-up symptom A44 exists to remove.
3. The exception path of the wait owns the reap too (the ``finally``).
4. The blocker of the first pass: ``setsid`` children were invisible. The
   stage now marks the env and the reap matches it — this file watches the
   whole chain (env → child setsid → reap), so a dropped ``stage_anchor=``
   cannot pass unnoticed.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from awf import agent_stage, worker_tree
from awf.pipeline import Stage

# A worker that forks a sleeping grandchild and exits: the observed shape.
WORKER_WITH_GRANDCHILD = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "sys.exit(0)\n"
)
# The real opencode shape: children leave the worker's session with setsid.
SETSID_TREE = (
    "import subprocess, sys\n"
    "for _ in range(2):\n"
    "    subprocess.Popen(\n"
    "        [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
    "        start_new_session=True,\n"
    "    )\n"
    "sys.exit(0)\n"
)
# A member that ignores TERM and keeps forking while it waits for KILL.
TERM_IGNORING_FORKER = (
    "import signal, subprocess, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: None)\n"
    "for _ in range(40):\n"
    "    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "    time.sleep(0.05)\n"
)


def _spawn(argv: list[str]) -> subprocess.Popen:
    return subprocess.Popen(argv, start_new_session=True)


def _wait_for_member(
    root_pid: int | None,
    pgid: int | None,
    token: str | None = None,
    timeout: float = 10.0,
) -> list[worker_tree.ProcInfo]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        members = worker_tree.live_members(root_pid, pgid, token)
        if members:
            return members
        time.sleep(0.05)
    return []


def _worker_with_grandchild() -> tuple[subprocess.Popen, list[worker_tree.ProcInfo]]:
    worker = _spawn([sys.executable, "-c", WORKER_WITH_GRANDCHILD])
    worker.wait(timeout=15)
    members = _wait_for_member(worker.pid, worker.pid)
    assert members, "the grandchild did not appear in the worker's group"
    return worker, members


def _stage_project(tmp_path):
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer role\n", encoding="utf-8"
    )
    return proj


def _fake_stage_spawn(monkeypatch, *, blow_up: bool = False) -> dict:
    """A real worker in its own group; the wait itself is a double."""
    state: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 on_spawn=None, **kwargs):
        worker, members = _worker_with_grandchild()
        state["worker"] = worker
        state["members"] = members
        if on_spawn is not None:
            on_spawn(worker.pid)
        if on_spawn is not None:
            on_spawn(worker.pid)
        if blow_up:
            raise RuntimeError("stage wait blew up")
        return type("_R", (), {"returncode": 0})()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return state


def test_stage_reaps_its_worker_tree_on_natural_exit(tmp_path, monkeypatch):
    """The unit's acceptance case — but through the stage, not the helper."""
    state = _fake_stage_spawn(monkeypatch)
    proj = _stage_project(tmp_path)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    try:
        agent_stage.run_agent_stage(
            stage, "TODO-0001", proj,
            {"models": {"worker": {"model": "p/test"}}},
            proj / ".agentic" / "logs"
        )

        pid = state["worker"].pid
        assert worker_tree.live_members(pid, pid) == []
        assert all(worker_tree._proc_info(i.pid) is None for i in state["members"])
        text = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
            encoding="utf-8"
        )
        assert "A44: reaping worker tree of developer/TODO-0001" in text
        assert "A44: worker tree reaped:" in text
    finally:
        worker_tree.reap_worker_tree(state["worker"].pid, state["worker"].pid, grace=2.0)


def test_stage_reaps_even_when_the_wait_raises(tmp_path, monkeypatch):
    """The reap lives in ``finally``: a stage that dies still leaves no tree."""
    state = _fake_stage_spawn(monkeypatch, blow_up=True)
    proj = _stage_project(tmp_path)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    try:
        with pytest.raises(RuntimeError):
            agent_stage.run_agent_stage(
                stage, "TODO-0002", proj,
                {"models": {"worker": {"model": "p/test"}}},
                proj / ".agentic" / "logs"
            )

        pid = state["worker"].pid
        assert worker_tree.live_members(pid, pid) == []
        assert all(worker_tree._proc_info(i.pid) is None for i in state["members"])
    finally:
        worker_tree.reap_worker_tree(state["worker"].pid, state["worker"].pid, grace=2.0)


def test_reap_catches_children_forked_during_the_grace_window(tmp_path):
    """A member that ignores TERM and forks on the way out must not leak.

    One snapshot misses every child born between the scan and the KILL; the
    group then keeps the pile-up A44 removes (the observed restart loop).
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    worker = _spawn([sys.executable, "-c", TERM_IGNORING_FORKER])
    time.sleep(0.5)
    try:
        report = worker_tree.reap_worker_tree(
            worker.pid, worker.pid, grace=0.5, logs_dir=logs
        )

        assert report.total >= 1
        leaked = worker_tree.live_members(worker.pid, worker.pid)
        assert leaked == [], f"leaked past the reap: {[i.pid for i in leaked]}"
        assert report.survivors == []
    finally:
        worker_tree.reap_worker_tree(worker.pid, worker.pid, grace=2.0)


def test_stage_env_marker_reaches_setsid_children(tmp_path, monkeypatch):
    """The whole chain: mark env → child setsid → reap still finds it.

    The blocker the first QA pass raised was that ``opencode serve --stdio``
    and the MCP servers leave the worker's session, so group+parentage lost
    them. A dropped ``stage_anchor=`` in the stage's env call would silently
    bring that defect back while the group tests stay green — this test
    watches the chain end to end.
    """
    state: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 on_spawn=None, **kwargs):
        token = (env or {}).get(worker_tree.ANCHOR_ENV)
        state["token"] = token
        assert token, "the stage did not put AWF_STAGE_ANCHOR into the env"
        worker = subprocess.Popen(
            [sys.executable, "-c", SETSID_TREE], start_new_session=True, env=env
        )
        state["worker"] = worker
        if on_spawn is not None:
            on_spawn(worker.pid)
        worker.wait(timeout=15)
        assert _wait_for_member(worker.pid, worker.pid, token=token), \
            "the marker did not reach the setsid children"
        return type("_R", (), {"returncode": 0})()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    proj = _stage_project(tmp_path)
    stage = Stage(name="agent-dev", role="developer", kind="execute")

    agent_stage.run_agent_stage(
        stage, "TODO-0003", proj,
        {"models": {"worker": {"model": "p/test"}}},
        proj / ".agentic" / "logs"
    )

    token = state["token"]
    assert worker_tree.live_members(None, None, token) == []
