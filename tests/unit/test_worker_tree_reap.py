"""TODO-0359 (A44): a finished stage leaves no live descendants.

Real subprocesses, not doubles: the defect is kernel process grouping
(``start_new_session``) and /proc parentage, and a fake Popen proves
nothing about either.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from awf import api, cmd_status, worker_tree
from awf.api._results import StatusResult
from awf.pipeline_state import write_state
from tests.unit._signal_fakes import make_project

# A worker that spawns a grandchild and exits immediately: exactly the
# observed shape — the engine's direct child is gone, its descendants are
# reparented and keep the worker's process group.
WORKER_WITH_GRANDCHILD = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "sys.exit(0)\n"
)
SLEEPER = "import time; time.sleep(120)"
# The real opencode shape: children leave the worker's session (setsid), so
# after the worker exits neither the group nor the parent link points at them.
SETSID_TREE = (
    "import subprocess, sys\n"
    "for _ in range(2):\n"
    "    subprocess.Popen(\n"
    "        [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
    "        start_new_session=True,\n"
    "    )\n"
    "sys.exit(0)\n"
)


def _spawn(argv: list[str], token: str | None = None) -> subprocess.Popen:
    env = None
    if token is not None:
        env = dict(os.environ)
        env[worker_tree.ANCHOR_ENV] = token
    return subprocess.Popen(argv, start_new_session=True, env=env)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _wait_for_member(
    root_pid: int, pgid: int, timeout: float = 10.0
) -> list[worker_tree.ProcInfo]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        members = worker_tree.live_members(root_pid, pgid)
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


def _kill_quietly(pid: int) -> None:
    worker_tree.reap_worker_tree(pid, pid, grace=2.0)


def test_worker_grandchild_dies_after_the_stage(tmp_path):
    """The acceptance case: worker + sleeping grandchild are both dead."""
    logs = tmp_path / "logs"
    logs.mkdir()
    worker, members = _worker_with_grandchild()
    try:
        report = worker_tree.reap_worker_tree(
            worker.pid, worker.pid, grace=2.0, logs_dir=logs
        )

        assert report.total >= 1
        assert worker_tree.live_members(worker.pid, worker.pid) == []
        assert all(
            worker_tree._proc_info(info.pid) is None for info in members
        )
        log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
        assert "A44: worker tree reaped:" in log_text
        assert "process(es)" in log_text
    finally:
        _kill_quietly(worker.pid)


def test_foreign_process_outside_the_tree_is_untouched(tmp_path):
    """A process in another group is never signaled, even with a live tree."""
    foreign = _spawn([sys.executable, "-c", SLEEPER])
    worker, members = _worker_with_grandchild()
    try:
        worker_tree.reap_worker_tree(worker.pid, worker.pid, grace=2.0)

        assert worker_tree._proc_info(foreign.pid) is not None
        assert foreign.poll() is None
        assert worker_tree.live_members(worker.pid, worker.pid) == []
    finally:
        _kill_quietly(foreign.pid)
        _kill_quietly(worker.pid)


def test_status_counts_live_worker_processes_then_zero(tmp_path):
    """``awf status`` sees the live tree and 0 after the reap."""
    proj = make_project(tmp_path)
    worker, members = _worker_with_grandchild()
    live_pid = members[0].pid
    write_state(
        proj,
        worker_pid=worker.pid,
        worker_pgid=worker.pid,
        worker_role="agent-implementer",
        worker_todo="TODO-0001",
    )
    try:
        status = api.get_status(proj)
        assert status.worker_processes is not None
        assert status.worker_processes["count"] == len(members)
        assert status.worker_processes["role"] == "agent-implementer"
        assert status.worker_processes["todo_id"] == "TODO-0001"

        worker_tree.reap_worker_tree(worker.pid, worker.pid, grace=2.0)

        status_after = api.get_status(proj)
        assert status_after.worker_processes is not None
        assert status_after.worker_processes["count"] == 0
        assert worker_tree._proc_info(live_pid) is None
    finally:
        _kill_quietly(worker.pid)


def test_cmd_status_prints_the_worker_tree_line(tmp_path, monkeypatch, capsys):
    """The CLI surface prints the count (0 included) — one line, no secrets."""
    fake = StatusResult(
        project_name="p",
        active_todos=[],
        done_count=0,
        blocked_count=0,
        blocked_ids=[],
        conflict_warning=None,
        suggestion=None,
        worker_processes={
            "count": 0,
            "pids": [],
            "role": "agent-implementer",
            "todo_id": "TODO-0001",
        },
    )
    monkeypatch.setattr(api, "get_status", lambda project_dir: fake)

    class Args:
        project_dir = str(tmp_path)

    assert cmd_status.run(Args()) == 0
    out = capsys.readouterr().out
    assert "Worker tree: 0 live process(es) (agent-implementer/TODO-0001)" in out


def test_leftover_check_names_the_previous_stage(tmp_path, monkeypatch):
    """A44: the next stage's start logs live tails of the previous tree."""
    proj = make_project(tmp_path)
    logs = proj / ".agentic" / "logs"
    worker, members = _worker_with_grandchild()
    write_state(
        proj,
        worker_pid=worker.pid,
        worker_pgid=worker.pid,
        worker_role="agent-qa-review",
        worker_todo="TODO-0002",
    )
    try:
        agent_stage_leftovers = worker_tree.log_stage_leftovers(proj, logs)
        assert agent_stage_leftovers == len(members)

        text = (logs / "orchestrator.log").read_text(encoding="utf-8")
        assert "A44: leftovers from the previous stage" in text
        assert "agent-qa-review/TODO-0002" in text
        assert f"{len(members)} live process(es)" in text
    finally:
        _kill_quietly(worker.pid)


def test_reap_refuses_pid_one_and_the_own_group():
    """Safety: the 2026-09-20 guard — nothing at or below pid 1, no own group."""
    report = worker_tree.reap_worker_tree(1, 1, grace=0.1)
    assert report.total == 0

    report_own = worker_tree.reap_worker_tree(
        os.getpid(), os.getpgrp(), grace=0.1
    )
    assert report_own.total == 0
    assert worker_tree.live_members(os.getpid(), os.getpgrp()) == []


@pytest.mark.parametrize("pid,pgid", [(None, None), (0, 0), (-1, -1)])
def test_unusable_anchors_yield_no_members(pid, pgid):
    assert worker_tree.live_members(pid, pgid) == []


def test_stage_anchor_reaps_only_its_own_spawn(tmp_path):
    """The stage exit path: no spawn → no reap; a spawn → its group dies."""
    logs = tmp_path / "logs"
    logs.mkdir()

    empty = worker_tree.SpawnAnchor()
    assert worker_tree.reap_stage_tree(empty, logs, label="role/TODO-0003").total == 0

    worker, members = _worker_with_grandchild()
    anchor = worker_tree.SpawnAnchor()
    anchor(worker.pid)
    assert anchor.usable()
    try:
        report = worker_tree.reap_stage_tree(
            anchor, logs, label="agent-implementer/TODO-0003"
        )
        assert report.total >= 1
        assert worker_tree.live_members(worker.pid, worker.pid) == []
        text = (logs / "orchestrator.log").read_text(encoding="utf-8")
        assert "A44: reaping worker tree of agent-implementer/TODO-0003" in text
    finally:
        _kill_quietly(worker.pid)


def test_setsid_descendants_are_found_and_reaped_by_the_marker(tmp_path):
    """The blocker QA found: group+parentage alone cannot see ``setsid`` kids.

    Red half: after the worker exits, the old anchors see nothing.
    Green half: the env marker keeps both children in reach and the reap
    kills them. This is the real ``opencode serve`` / MCP-server shape.
    """
    logs = tmp_path / "logs"
    logs.mkdir()
    token = worker_tree.new_anchor_token()
    worker = _spawn([sys.executable, "-c", SETSID_TREE], token)
    worker.wait(timeout=15)

    assert _wait_until(
        lambda: len(worker_tree.live_members(worker.pid, worker.pid, token)) >= 2
    ), "the setsid children never appeared"

    # The old anchor pair is blind to them — this is the defect, not a slip.
    assert worker_tree.live_members(worker.pid, worker.pid) == []
    # The state's token alone is enough (status/leftovers path).
    assert worker_tree.live_members_of_state({"worker_anchor": token})

    try:
        report = worker_tree.reap_worker_tree(
            worker.pid, worker.pid, token=token, grace=2.0, logs_dir=logs
        )

        assert report.total >= 2
        assert worker_tree.live_members(worker.pid, worker.pid, token) == []
        text = (logs / "orchestrator.log").read_text(encoding="utf-8")
        assert "A44: worker tree reaped:" in text
    finally:
        worker_tree.reap_worker_tree(worker.pid, worker.pid, token=token, grace=2.0)


def test_another_stages_marker_is_untouched(tmp_path):
    """Parallel stages: only the exact token matches; a foreign tree survives."""
    ours = worker_tree.new_anchor_token()
    theirs = worker_tree.new_anchor_token()
    foreign = _spawn([sys.executable, "-c", SLEEPER], theirs)
    worker = _spawn([sys.executable, "-c", SETSID_TREE], ours)
    worker.wait(timeout=15)
    try:
        assert _wait_until(
            lambda: len(worker_tree.live_members(worker.pid, worker.pid, ours)) >= 2
        )
        worker_tree.reap_worker_tree(
            worker.pid, worker.pid, token=ours, grace=2.0
        )

        assert worker_tree.live_members(worker.pid, worker.pid, ours) == []
        assert worker_tree._proc_info(foreign.pid) is not None
        assert foreign.poll() is None
    finally:
        worker_tree.reap_worker_tree(worker.pid, worker.pid, token=ours, grace=2.0)
        worker_tree.reap_worker_tree(foreign.pid, foreign.pid, token=theirs, grace=2.0)
