"""A44 (TODO-0359): the stage marker never rides into a detached child.

CTX1 from the review: ``AWF_STAGE_ANCHOR`` in the parent env leaked into
``awf start --background`` (the normal MCP-tool path) and into the detached
``suite-canonical.sh`` run. Those are NOT part of the stage that launched
them, so the stage-exit reap would kill them — a regression A44 introduced.
The three surfaces below must strip the marker; status must use the one
anchor rule (CTX2), not a copy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from subprocess import CompletedProcess

from awf import api, worker_tree
from awf.api import _background
from awf.pipeline_state import write_state
from tests.unit._signal_fakes import make_project

REPO_ROOT = Path(__file__).resolve().parents[2]
DETACHED_SUITE_SCRIPTS = (
    REPO_ROOT / "scripts" / "suite-canonical.sh",
    REPO_ROOT / "awf" / "templates" / "scripts" / "suite-canonical.sh",
)


def test_background_launch_strips_the_stage_marker(tmp_path, monkeypatch):
    """`awf start --background` from a worker must outlive that worker."""
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    monkeypatch.setenv(worker_tree.ANCHOR_ENV, "outer-stage-token")

    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProc()

    monkeypatch.setattr(_background.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_background, "_reap_in_background", lambda proc, logs: None)

    _background.start_in_background(
        proj,
        pipeline=None,
        from_stage=None,
        auto=True,
        timeout=None,
    )

    assert worker_tree.ANCHOR_ENV not in captured["env"]
    assert captured["env"].get("AWF_BACKGROUND_CHILD") == "1"


def test_build_background_child_env_drops_the_anchor(monkeypatch):
    """CTX1 unit surface: the detached child's env is built in one place."""
    monkeypatch.setenv(worker_tree.ANCHOR_ENV, "outer-stage-token")
    monkeypatch.setenv("AWF_NO_CHECKPOINTS", "1")

    env = _background.build_background_child_env()

    assert worker_tree.ANCHOR_ENV not in env
    assert env.get("AWF_BACKGROUND_CHILD") == "1"
    assert "AWF_NO_CHECKPOINTS" not in env


def test_extra_cannot_smuggle_the_marker_back(monkeypatch):
    """The drop happens at the boundary, so ``extra`` cannot undo it."""
    monkeypatch.setenv(worker_tree.ANCHOR_ENV, "outer-stage-token")

    env = _background.build_background_child_env(
        {worker_tree.ANCHOR_ENV: "inner"}
    )

    assert worker_tree.ANCHOR_ENV not in env


def test_detached_suite_scripts_drop_the_marker():
    """The `setsid nohup` restart must not inherit the launcher's stage."""
    for script in DETACHED_SUITE_SCRIPTS:
        # Only the live invocation counts: a commented-out row would satisfy
        # a plain substring check while the real restart keeps the marker.
        line = next(
            row for row in script.read_text(encoding="utf-8").splitlines()
            if "setsid nohup" in row and not row.lstrip().startswith("#")
        )
        assert "env -u AWF_STAGE_ANCHOR" in line, f"{script}: {line}"


def test_without_anchor_leaves_other_env_untouched():
    env = {worker_tree.ANCHOR_ENV: "t", "PATH": "/bin"}

    assert worker_tree.without_anchor(env) == {"PATH": "/bin"}
    assert worker_tree.without_anchor({}) == {}


def test_status_ignores_a_bogus_worker_pid(tmp_path):
    """CTX2: status takes the anchor rule from worker_tree, not a copy."""
    proj = make_project(tmp_path)

    write_state(proj, worker_pid=1, worker_pgid=1, worker_role="x")
    assert api.get_status(proj).worker_processes is None

    write_state(proj, worker_pid=os.getpid(), worker_pgid=os.getpgrp())
    status = api.get_status(proj)
    # The caller's own group is never "the worker tree" — the guard refuses.
    assert status.worker_processes is not None
    assert status.worker_processes["count"] == 0


def test_status_asks_the_shared_anchor_rule(tmp_path, monkeypatch):
    """CTX2: the status surface calls the owner instead of re-deriving it."""
    proj = make_project(tmp_path)
    # A pid that is not ours and a group that is ours: only the shared rule
    # (refuse the caller's own group) yields an empty tree here.
    write_state(proj, worker_pid=os.getpid() + 1, worker_pgid=os.getpgrp())

    seen: list = []
    original = worker_tree.state_anchor

    def spy(state):
        seen.append(state)
        return original(state)

    monkeypatch.setattr(worker_tree, "state_anchor", spy)
    status = api.get_status(proj)

    assert seen, "status did not ask worker_tree.state_anchor"
    assert status.worker_processes is not None
    assert status.worker_processes["count"] == 0


# --- QA (stage agent-qa-review): the consequence, not just the env dict ------
#
# The tests above assert that the key is absent from a dict. Real /proc and a
# real reap are what the owner cares about: the detached child is alive after
# the stage that launched it exits. These spawn real processes — a fake Popen
# cannot prove the kernel-side effect (same rule as test_worker_tree_reap).


def _spawn_detached(env: dict[str, str]):
    import subprocess
    import sys

    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=env,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _carries_marker(pid: int, token: str) -> bool:
    needle = f"{worker_tree.ANCHOR_ENV}={token}".encode()
    return needle in Path(f"/proc/{pid}/environ").read_bytes()


def _wait_exit(proc, timeout: float = 5.0) -> bool:
    try:
        proc.wait(timeout=timeout)
    except Exception:  # noqa: BLE001 — any wait failure means "still there"
        return False
    return True


def test_detached_child_survives_the_outer_stage_reap(monkeypatch):
    """CTX1, consequence level: the stage-exit reap leaves it alone."""
    token = worker_tree.new_anchor_token()
    monkeypatch.setenv(worker_tree.ANCHOR_ENV, token)

    proc = _spawn_detached(_background.build_background_child_env())
    try:
        assert not _carries_marker(proc.pid, token)
        report = worker_tree.reap_worker_tree(None, None, token=token, grace=0.2)
        assert report.killed == []
        assert report.survivors == []
        assert proc.poll() is None, "the detached child died with the stage"
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_the_same_reap_kills_a_child_that_keeps_the_marker(monkeypatch):
    """Vacuum guard: without the drop the reap DOES collect the child.

    If this goes green-by-accident the test above proves nothing — the reap
    stopped seeing marker-carrying processes at all.
    """
    token = worker_tree.new_anchor_token()
    monkeypatch.setenv(worker_tree.ANCHOR_ENV, token)
    leaky = {**os.environ, "AWF_BACKGROUND_CHILD": "1"}

    proc = _spawn_detached(leaky)
    try:
        assert _carries_marker(proc.pid, token)
        report = worker_tree.reap_worker_tree(None, None, token=token, grace=0.5)
        assert proc.pid in [info.pid for info in report.killed]
        assert _wait_exit(proc), "a marker-carrying child survived the reap"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


def test_no_checkpoints_flag_reaches_the_built_env(monkeypatch):
    """RUN3 #6 ordering: the parent's flag is dropped, the launcher's is not.

    The drop runs before ``extra`` is merged; flip that order and every
    ``--no-checkpoints`` background launch silently loses its bypass.
    """
    monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

    env = _background.build_background_child_env({"AWF_NO_CHECKPOINTS": "1"})

    assert env.get("AWF_NO_CHECKPOINTS") == "1"


def test_stage_wiring_token_to_reap_end_to_end(tmp_path, monkeypatch):
    """Auditor MAJOR: the whole chain stage → env → state → reap.

    A real worker receives the stage's env, its ``setsid`` child inherits the
    marker, the state records that same token, and the ``finally`` reap kills
    the child. No double in the middle: a broken ``stage_anchor=`` argument
    or a state key typo turns this red.
    """
    from awf import agent_stage
    from awf.pipeline import Stage
    from awf.pipeline_state import read_state

    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles", "context"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  agent-dev: execute\n", encoding="utf-8"
    )
    (proj / ".agentic" / "roles" / "developer.md").write_text("# Role\n")

    captured: dict = {}

    def fake_run(cmd, cwd=None, **kwargs):
        env = kwargs.get("env") or {}
        token = env[worker_tree.ANCHOR_ENV]
        captured["token"] = token
        worker = subprocess.Popen(
            [sys.executable, "-c", SETSID_WORKER],
            env=env,
            start_new_session=True,
        )
        on_spawn = kwargs.get("on_spawn")
        if on_spawn is not None:
            on_spawn(worker.pid)
        worker.wait(timeout=15)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            members = worker_tree.live_members(None, None, token)
            if members:
                captured["child"] = members[0].pid
                break
            time.sleep(0.05)
        return CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)

    stage = Stage(name="agent-dev", role="developer", kind="execute")
    agent_stage.run_agent_stage(
        stage, "TODO-0001", proj,
        {"models": {"worker": {"model": "p/test"}}},
        proj / ".agentic" / "logs"
    )

    token = captured["token"]
    assert read_state(proj)["worker_anchor"] == token
    child = captured.get("child")
    assert child is not None, "the setsid child never inherited the marker"
    assert worker_tree._proc_info(child) is None, "the stage reap missed it"


SETSID_WORKER = (
    "import subprocess, sys\n"
    "subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(120)'],\n"
    "    start_new_session=True,\n"
    ")\n"
    "sys.exit(0)\n"
)


def test_background_launch_passes_no_checkpoints_to_the_spawned_child(
    tmp_path, monkeypatch
):
    """The call site actually asks for the flag — not only the builder."""
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
    captured: dict = {}

    class _FakeProc:
        pid = 4243

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs["env"]
        return _FakeProc()

    monkeypatch.setattr(_background.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_background, "_reap_in_background", lambda proc, logs: None)

    _background.start_in_background(
        proj,
        pipeline=None,
        from_stage=None,
        auto=True,
        timeout=None,
        no_checkpoints=True,
    )

    assert captured["env"].get("AWF_NO_CHECKPOINTS") == "1"
