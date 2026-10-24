"""TODO-0242: carry batch A+B — engine liveness, queue and tools.

The two blocked audits (0240/0241) left defects that are cheaper to prove
together than one by one, plus the 2026-10-23 incident (the engine died six
times with its parent).

Cases (the TODO's acceptance list):
(a) parent-death is opt-in: default does not arm it, config arms it;
(b) heartbeat config: 600 -> 30 with a warning, stale = max(3x, 120);
(c) ``wait_for_event`` answers ``engine_dead`` (not timeout) and keeps the
    old behaviour on a live pid;
(d) two ENGINE-EXIT records in one second, rc-only text, retention;
(e) one libc helper for ``_env`` and ``_engine_liveness`` (patched CDLL);
(f) a refused operation id does not stick — the same id works after the fix;
(g) ``list_orphans`` does not offer a queued unit;
(h) closure matrix: placeholder row is red, a data row is green;
(i) queue revision: ``after`` anchor and ``remove``;
(j) dispatch size warning: 7 requirements yes, 5 no;
(P0a) the engine survives its parent (real subprocess);
(P0b) ``awf supervise`` restarts within a bounded budget and stops;
(P0c) an execute worker config keeps only the allowed MCP servers;
(P0d) old configs without the new keys still work.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
T1 = "TODO-0001"
T2 = "TODO-0002"
T3 = "TODO-0003"
T4 = "TODO-0004"
T5 = "TODO-0005"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("state", "context", "logs", "inbox", "outbox"):
        (project / ".agentic" / name).mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: CarryBatch\n", encoding="utf-8"
    )
    return project


def _write_todo(project: Path, todo_id: str = T1) -> None:
    inbox = project / ".agentic" / "inbox"
    (inbox / f"{todo_id}.md").write_text(
        f"# {todo_id}\n\n## Требования\n1. do it\n", encoding="utf-8"
    )
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _dead_pid() -> int:
    return 2**31


# ─── (a) parent-death is opt-in ─────────────────────────────────────────


def test_default_engine_does_not_arm_parent_death(tmp_path, monkeypatch):
    """P0-1: a durable run must outlive its launcher (incident 2026-10-23)."""
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(
        lv, "install_parent_death_signal",
        lambda *a, **k: calls.append(1) or True,
    )
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)

    engine = lv.EngineLiveness(project, {}, interval=0.05).start()
    try:
        assert engine._kill_with_parent is False
        assert calls == [], "PR_SET_PDEATHSIG must not be armed by default"
        assert engine._pdeath is False
    finally:
        engine.finish(0)


def test_kill_with_parent_true_arms_the_signal(tmp_path, monkeypatch):
    """A.1: the explicit opt-in still arms the signal (both paths)."""
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    calls: list[int] = []
    monkeypatch.setattr(
        lv, "install_parent_death_signal",
        lambda *a, **k: calls.append(1) or True,
    )
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)

    engine = lv.EngineLiveness(
        project, {"engine": {"kill_with_parent": True}}, interval=0.05
    ).start()
    try:
        assert calls == [1]
        assert engine._pdeath is True
    finally:
        engine.finish(0)


def test_finish_disarms_the_parent_death_signal(tmp_path, monkeypatch):
    """A.1: a clean finish clears PR_SET_PDEATHSIG — it is not a death."""
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    monkeypatch.setattr(lv, "install_parent_death_signal", lambda *a, **k: True)
    disarmed: list[int] = []
    monkeypatch.setattr(
        lv, "disarm_parent_death_signal",
        lambda *a, **k: disarmed.append(1) or True,
    )
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)

    engine = lv.EngineLiveness(
        project, {"engine": {"kill_with_parent": True}}, interval=0.05
    ).start()
    assert engine._pdeath is True
    engine.finish(0)
    assert disarmed == [1], "finish must call prctl(PR_SET_PDEATHSIG, 0)"
    assert engine._pdeath is False

    # An engine that never armed the signal has nothing to disarm.
    monkeypatch.setattr(lv, "install_parent_death_signal", lambda *a, **k: False)
    plain = lv.EngineLiveness(project, {}, interval=0.05).start()
    plain.finish(0)
    assert disarmed == [1]


def test_parent_death_race_before_prctl_is_caught(tmp_path, monkeypatch):
    """A.1: the signal is not retroactive — the ppid check catches the race.

    The parent may die between process start and ``prctl``; the armed
    signal would never fire for that window, so ``start`` re-reads
    ``getppid`` once right after arming and records the death itself.
    """
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)
    monkeypatch.setattr(lv, "install_parent_death_signal", lambda *a, **k: True)
    engine = lv.EngineLiveness(
        project, {"engine": {"kill_with_parent": True}}, interval=0.05
    )
    real_ppid = engine._initial_ppid

    def _fake_exit(code):
        raise SystemExit(code)

    monkeypatch.setattr(lv.os, "getppid", lambda: real_ppid + 1)
    monkeypatch.setattr(lv.os, "_exit", _fake_exit)
    with pytest.raises(SystemExit):
        engine.start()
    assert engine._finished is True
    # A clean finish already disarmed the signal; a raced one exits after
    # recording, so nothing in the context dir may claim a live engine.
    exits = list((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert exits, "the raced parent death must leave an exit record"
    assert "parent died" in exits[0].read_text(encoding="utf-8")


def test_engine_survives_its_parent(tmp_path):
    """P0a: the parent dies by SIGTERM — the detached engine keeps running."""
    project = _project(tmp_path)
    engine_pid_file = tmp_path / "engine.pid"
    engine_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from awf._engine_liveness import EngineLiveness\n"
        f"lv = EngineLiveness(Path(r'{project}'), {{}}, interval=0.05).start()\n"
        f"Path(r'{engine_pid_file}').write_text(str(__import__('os').getpid()))\n"
        "time.sleep(30)\n"
    )
    launcher_code = (
        "import os, signal, subprocess, sys\n"
        f"proc = subprocess.Popen([sys.executable, '-c', {engine_code!r}],"
        " start_new_session=True,"
        " env={**os.environ, 'PYTHONPATH': "
        f"{str(ROOT)!r}}})\n"
        f"Path(r'{engine_pid_file}').write_text(str(proc.pid))\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    # Path is used by the launcher child; keep the import explicit there.
    launcher_code = (
        "from pathlib import Path\n" + launcher_code
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    subprocess.run(
        [sys.executable, "-c", launcher_code],
        env=env,
        timeout=60,
        check=False,
    )
    for _ in range(100):
        if engine_pid_file.is_file():
            break
        time.sleep(0.05)
    pid = int(engine_pid_file.read_text())
    try:
        time.sleep(0.5)
        os.kill(pid, 0)  # raises when the engine died with the parent
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


# ─── (b) heartbeat configuration ────────────────────────────────────────


def test_heartbeat_interval_is_clamped_and_stale_follows_the_formula():
    from awf import _engine_liveness as lv

    with pytest.warns(RuntimeWarning, match="ceiling"):
        assert lv.heartbeat_interval_seconds(
            {"engine": {"heartbeat_seconds": 600}}
        ) == 30.0
    # An explicit stale value is honoured; the default is the formula.
    assert lv.heartbeat_stale_seconds(
        {"engine": {"heartbeat_seconds": 600, "heartbeat_stale_seconds": 300}}
    ) == 300.0
    assert lv.heartbeat_stale_seconds({"engine": {"heartbeat_seconds": 10}}) == 120.0
    assert lv.heartbeat_stale_seconds({"engine": {"heartbeat_seconds": 30}}) == 120.0


# ─── (c) wait_for_event: engine_dead, not timeout ───────────────────────


def test_wait_for_event_reports_engine_dead(tmp_path):
    from awf import pipeline_state
    from awf.api import wait_event

    project = _project(tmp_path)
    pipeline_state.write_state(
        project,
        stage_name="agent-implementer",
        todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    result = wait_event.wait_for_event(project, timeout=2, poll_interval=1)
    assert result.event_type == "engine_dead"
    assert f"awf_continue(todo_id='{T1}')" in result.next_action


def test_wait_for_event_keeps_the_old_answer_on_a_live_pid(tmp_path):
    from awf import pipeline_state
    from awf.api import wait_event

    project = _project(tmp_path)
    pipeline_state.write_state(
        project,
        stage_name="agent-implementer",
        todo_id=T1,
        pipeline_pid=os.getpid(),
    )
    result = wait_event.wait_for_event(project, timeout=1, poll_interval=1)
    assert result.event_type != "engine_dead"


# ─── (d) ENGINE-EXIT naming, text and retention ─────────────────────────


def test_two_engine_exits_in_one_second_and_retention(tmp_path):
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    lv.write_engine_exit(project, rc=1, stage="s", todo_id=T1)
    lv.write_engine_exit(project, rc=1, stage="s", todo_id=T1)
    records = sorted((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert len(records) == 2, "a crash-loop overwrote its own evidence"
    assert "(no exception)" in records[0].read_text(encoding="utf-8")

    for _ in range(lv.ENGINE_EXIT_RETENTION + 3):
        lv.write_engine_exit(project, rc=0, stage="s")
    kept = list((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert len(kept) == lv.ENGINE_EXIT_RETENTION


def test_engine_exit_retention_refuses_a_symlinked_context(tmp_path):
    """The prune is a DELETE under ``.agentic/context``: a symlinked surface
    is refused by the managed no-follow walk instead of unlinking outside."""
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    keep = outside / "ENGINE-EXIT-keep.md"
    keep.write_text("keep", encoding="utf-8")
    for i in range(lv.ENGINE_EXIT_RETENTION + 2):
        (outside / f"ENGINE-EXIT-old-{i:02d}.md").write_text(
            "old", encoding="utf-8"
        )

    context = project / ".agentic" / "context"
    for child in context.iterdir():
        child.unlink()
    context.rmdir()
    context.symlink_to(outside, target_is_directory=True)

    lv._prune_engine_exits(project, keep=keep)

    assert len(list(outside.glob("ENGINE-EXIT-*.md"))) == (
        lv.ENGINE_EXIT_RETENTION + 3
    ), "the prune followed a symlinked context outside the project"


# ─── (e) one libc helper ────────────────────────────────────────────────


def test_one_libc_helper_for_both_modules(monkeypatch):
    import awf._libc as libc_mod
    from awf import _engine_liveness as lv
    from awf import _env

    calls: list[tuple] = []

    class FakeLibc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    fake = FakeLibc()
    monkeypatch.setattr(libc_mod.ctypes, "CDLL", lambda *a, **k: fake)
    libc_mod.reset_cache()
    try:
        assert libc_mod.get_libc() is fake
        assert lv.install_parent_death_signal(platform="linux") is True
        _env._pdeathsig_preexec()
        assert len(calls) == 2, "both callers must use the one helper"
    finally:
        libc_mod.reset_cache()


# ─── (f) a refusal is not final ────────────────────────────────────────


def test_refused_operation_id_works_after_the_cause_is_gone(tmp_path, monkeypatch):
    from awf import api
    from awf.api import hygiene

    project = _project(tmp_path)
    _write_todo(project)
    (project / ".agentic" / "outbox" / f"PROGRESS-{T1}.md").write_text(
        "stage: dev\n", encoding="utf-8"
    )

    monkeypatch.setattr(hygiene, "check_pipeline_running", lambda _p: (True, 4242, "x"))
    monkeypatch.setattr(hygiene, "read_state", lambda _p: {"todo_id": T1})
    with pytest.raises(api.AwfApiError, match="not final"):
        api.review_append(project, T1, "fix", operation_id="ra-retry")

    monkeypatch.setattr(hygiene, "check_pipeline_running", lambda _p: (False, 0, ""))
    monkeypatch.setattr(hygiene, "read_state", lambda _p: {})
    result = api.review_append(project, T1, "fix", operation_id="ra-retry")
    assert "REVIEW-фикс" in result.section


# ─── (g) list_orphans agrees with remove_orphans ────────────────────────


def test_list_orphans_skips_a_queued_unit(tmp_path):
    from awf import api, run_state

    project = _project(tmp_path)
    _write_todo(project, T1)
    _write_todo(project, T2)
    run_state.write_run(
        project,
        active=True,
        queue=[{"todo_id": T1, "pipeline": ""}],
        index=0,
        current="",
        generation=1,
    )
    assert api.list_orphans(project) == [T2]


# ─── (h) closure matrix: a real data row or red ─────────────────────────


def _matrix_project(tmp_path: Path, row: str) -> Path:
    tree = tmp_path / "tree"
    (tree / "docs" / "contracts").mkdir(parents=True)
    (tree / "x.py").write_text("foo = 1\n", encoding="utf-8")
    (tree / "docs" / "contracts" / "x.md").write_text(
        "# Contract: X\nOwns: x\nPath: `foo`\nNever: x\nGate: review\n",
        encoding="utf-8",
    )
    inbox = tree / ".agentic" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / f"{T1}.md").write_text(
        "# T\n\n## Границы\n`unlink` in `x.py`\n\n"
        "## Матрица замыкания\n\n"
        "| Операция | Поверхность | Корень/лист | Порядок |\n"
        "|---|---|---|---|\n"
        f"{row}\n",
        encoding="utf-8",
    )
    return tree


def _run_contracts(tree: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", str(ROOT / "scripts" / "check-contracts.sh"), "docs/contracts"],
        cwd=str(tree),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_matrix_placeholder_row_is_red(tmp_path):
    tree = _matrix_project(tmp_path, "| TODO | ... | — | ? |")
    result = _run_contracts(tree)
    assert result.returncode != 0
    assert "placeholder" in result.stdout.lower()


def test_matrix_data_row_is_green(tmp_path):
    tree = _matrix_project(
        tmp_path, "| unlink | `x.py` | лист | после проверки |"
    )
    result = _run_contracts(tree)
    assert result.returncode == 0, result.stdout


# ─── (i) queue anchors and remove ───────────────────────────────────────


def _run_with_queue(project: Path, ids: list[str]) -> None:
    from awf import run_state

    for tid in ids:
        _write_todo(project, tid)
    run_state.write_run(
        project,
        active=True,
        queue=[{"todo_id": tid, "pipeline": ""} for tid in ids],
        index=0,
        current="",
        generation=1,
    )


def _queue_ids(project: Path) -> list[str]:
    from awf import run_state

    return [q["todo_id"] for q in run_state.read_run(project)["queue"]]


def test_queue_after_anchor_and_remove(tmp_path):
    from awf import api

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2, T3])

    _write_todo(project, T4)
    added = api.run_revise(
        project, queue=[{"todo_id": T4}], after=T1, key="after-2"
    )
    assert added.action == "applied", added.message
    assert _queue_ids(project) == [T1, T4, T2, T3]

    removed = api.run_revise(project, queue=[{"remove": T2}], key="remove-1")
    assert removed.action == "applied", removed.message
    assert _queue_ids(project) == [T1, T4, T3]

    _write_todo(project, T5)
    before = api.run_revise(
        project, queue=[{"todo_id": T5}], before=T3, key="before-1"
    )
    assert before.action == "applied", before.message
    assert _queue_ids(project) == [T1, T4, T5, T3]


def test_removal_and_insert_share_one_position_owner(tmp_path):
    """architect-review (Q1): the answer and the stored queue agree.

    A removal standing AT the base is not "before" it — counting on the
    mutating queue pulled later indices into the region and under-shifted
    the insert by one. One helper owns the shift for both.
    """
    from awf import api

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2, T3, T4])
    _write_todo(project, T5)
    result = api.run_revise(
        project,
        queue=[{"todo_id": T5}, {"remove": T1}, {"remove": T4}],
        position=3,
        key="shift-one-owner",
    )
    assert result.action == "applied", result.message
    assert "inserted at position 2" in result.message
    assert _queue_ids(project) == [T2, T3, T5]


def test_queue_anchor_and_position_are_mutually_exclusive(tmp_path):
    from awf import api

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2])
    _write_todo(project, T4)
    result = api.run_revise(
        project, queue=[{"todo_id": T4}], position=0, after=T1, key="both"
    )
    assert result.action == "refused"
    assert "mutually exclusive" in result.message


def test_queue_remove_of_a_started_element_is_a_conflict(tmp_path):
    from awf import api, run_state

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2])
    state = run_state.read_run(project)
    run_state.write_run(
        project,
        active=True,
        queue=state["queue"],
        index=1,
        current=T1,
        generation=state.get("generation", 1),
    )
    result = api.run_revise(project, queue=[{"remove": T1}], key="rm-started")
    assert result.action == "refused"
    assert any(c["todo_id"] == T1 for c in result.conflicts)


# ─── (j) dispatch size warning ──────────────────────────────────────────


def test_dispatch_size_warning_at_seven_requirements(tmp_git_repo):
    from awf import api
    from awf.api.dispatch import dispatch_todo

    api.init_project(tmp_git_repo, project_name="SizeWarn")
    seven = "# Big\n\n## Требования\n" + "\n".join(
        f"{i}. requirement" for i in range(1, 8)
    ) + "\n\n## Границы\n`awf/a.py`\n"
    result = dispatch_todo(tmp_git_repo, seven)
    assert result.size_warning, "7 requirements must warn"


def test_dispatch_size_warning_absent_at_five_requirements(tmp_git_repo):
    from awf import api
    from awf.api.dispatch import dispatch_todo

    api.init_project(tmp_git_repo, project_name="SizeOk")
    five = "# Small\n\n## Требования\n" + "\n".join(
        f"{i}. requirement" for i in range(1, 6)
    ) + "\n\n## Границы\n`awf/a.py`\n"
    result = dispatch_todo(tmp_git_repo, five)
    assert result.size_warning == ""


# ─── (P0b) awf supervise ───────────────────────────────────────────────


def test_supervise_restarts_until_the_budget_then_stops(tmp_path, monkeypatch):
    from awf import pipeline_state, run_state
    from awf.api import pipeline, supervise

    project = _project(tmp_path)
    _write_todo(project, T1)
    run_state.write_run(
        project,
        active=True,
        queue=[{"todo_id": T1, "pipeline": ""}],
        index=0,
        current=T1,
        generation=1,
    )
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        pipeline, "continue_pipeline",
        lambda *a, **k: calls.append(k) or {"run_mode": "background"},
    )
    result = supervise.supervise_run(
        project, max_restarts=2, pause_seconds=0, iterations=6,
        sleep=lambda _s: None,
    )
    assert result["action"] == "exhausted"
    assert result["restarts"] == 2
    assert len(calls) == 2
    assert all(c["todo_id"] == T1 for c in calls)
    log = (project / ".agentic" / "logs" / "awf-supervise.log").read_text()
    assert "supervise: restart #1" in log
    assert "supervise: restart #2" in log


def test_supervise_stops_when_the_run_is_gone(tmp_path):
    from awf.api import supervise

    project = _project(tmp_path)
    result = supervise.supervise_run(project, iterations=2, sleep=lambda _s: None)
    assert result["action"] == "no_run"
    assert result["restarts"] == 0


# ─── (P0c) worker MCP isolation ────────────────────────────────────────


def test_execute_worker_keeps_only_allowed_mcp_servers(tmp_path, monkeypatch):
    from awf import _env, xdg

    project = _project(tmp_path)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(
        json.dumps(
            {"mcp": {"agent-workflow-ui": {"enabled": True}, "heavy": {"enabled": True}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    env = _env.awf_subprocess_env(
        role="worker", project_dir=project, isolate_mcp=True
    )
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["heavy"]["enabled"] is False
    assert "agent-workflow-ui" not in payload["mcp"]


def test_unknown_worker_mcp_name_is_a_warning_not_a_failure(tmp_path, monkeypatch):
    from awf import _env, xdg

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "worker:\n  mcp_servers: [agent-workflow-ui, typo-server]\n",
        encoding="utf-8",
    )
    config_file = tmp_path / "opencode.json"
    config_file.write_text(
        json.dumps({"mcp": {"agent-workflow-ui": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    with pytest.warns(RuntimeWarning, match="typo-server"):
        env = _env.awf_subprocess_env(
            role="worker", project_dir=project, isolate_mcp=True
        )
    assert "OPENCODE_CONFIG_CONTENT" in env


def test_worker_config_without_the_new_keys_stays_workspace_wide(tmp_path, monkeypatch):
    from awf import _env, xdg

    project = _project(tmp_path)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(json.dumps({"mcp": {"heavy": {"enabled": True}}}),
                           encoding="utf-8")
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    env = _env.awf_subprocess_env(role="worker", project_dir=project)
    assert "mcp" not in json.loads(env["OPENCODE_CONFIG_CONTENT"])


def test_empty_worker_mcp_list_means_no_servers(tmp_path, monkeypatch):
    """ARCH: an EXPLICIT empty list is a value, not an absence.

    ``worker.mcp_servers: []`` is the only way to say "no MCP server for
    the execute worker"; falling back to the default made the option unable
    to express it (a silent surprise, not a default).
    """
    from awf import _env, xdg

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "worker:\n  mcp_servers: []\n", encoding="utf-8"
    )
    config_file = tmp_path / "opencode.json"
    config_file.write_text(
        json.dumps(
            {"mcp": {"agent-workflow-ui": {"enabled": True},
                     "heavy": {"enabled": True}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    env = _env.awf_subprocess_env(
        role="worker", project_dir=project, isolate_mcp=True
    )
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["agent-workflow-ui"]["enabled"] is False
    assert payload["mcp"]["heavy"]["enabled"] is False


# ─── (P0d) old configs keep working ────────────────────────────────────


def test_old_config_without_engine_keys_uses_defaults(tmp_path):
    from awf import _engine_liveness as lv

    assert lv.heartbeat_interval_seconds({}) == 30.0
    assert lv.heartbeat_stale_seconds({}) == 120.0
    project = _project(tmp_path)
    engine = lv.EngineLiveness(project, {}, interval=0.05).start()
    try:
        assert lv.read_heartbeat(project) is not None
        assert engine._pdeath is False
    finally:
        engine.finish(0)


# ─── A.3 / A.6: the fact and the advice on every surface ───────────────


def test_supervisor_context_carries_engine_dead(tmp_path):
    from awf import api, pipeline_state

    project = _project(tmp_path)
    _write_todo(project, T1)
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    context = api.load_supervisor_context(project)
    assert "dead" in context.engine_dead
    assert T1 in context.engine_dead_todo
    assert f"awf_continue(todo_id='{T1}')" in context.next_action


def test_inactive_run_does_not_advise_a_resume(tmp_path):
    import asyncio

    from awf import pipeline_state, run_state

    try:
        from agent_workflow_ui.tools import awf as tools
    except ImportError:  # pragma: no cover — plugin not installed
        pytest.skip("agent_workflow_ui not installed")

    project = _project(tmp_path)
    run_state.write_run(
        project,
        active=False,
        queue=[{"todo_id": T1, "pipeline": ""}],
        index=0,
        current="",
        stop_reason="done",
        generation=1,
    )
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    result = asyncio.run(tools.awf_run_status(project_dir=str(project)))
    assert result.get("status") == "ok", result
    assert "Run finished" in result.get("message", "")
    assert "awf_continue" not in result.get("next_action", "")
    assert "awf_run_start" in result.get("next_action", "")


def test_queue_anchor_repeat_with_the_same_key_is_a_noop(tmp_path):
    from awf import api

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2])
    _write_todo(project, T4)
    first = api.run_revise(
        project, queue=[{"todo_id": T4}], after=T1, key="anchor-idem"
    )
    assert first.action == "applied"
    again = api.run_revise(
        project, queue=[{"todo_id": T4}], after=T1, key="anchor-idem"
    )
    assert again.action == "noop"
    assert _queue_ids(project) == [T1, T4, T2]


# ─── REVIEW виток 5 (F5–F8): the queue/tools defects the QA round found ──


def test_removing_every_queued_element_is_refused(tmp_path):
    """F5: ``queue: []`` with ``active: true`` is unreadable — refuse."""
    from awf import api, run_state

    project = _project(tmp_path)
    _run_with_queue(project, [T1, T2])
    result = api.run_revise(
        project, queue=[{"remove": T1}, {"remove": T2}], key="f5-all"
    )
    assert result.action == "refused", result.message
    assert "last queued element" in result.message
    state = run_state.read_run(project)
    assert state is not None and state.get("active")
    assert _queue_ids(project) == [T1, T2]


def test_removing_the_only_element_is_refused(tmp_path):
    """F5b: the one-element boundary of the same invariant."""
    from awf import api, run_state

    project = _project(tmp_path)
    _run_with_queue(project, [T1])
    result = api.run_revise(project, queue=[{"remove": T1}], key="f5-one")
    assert result.action == "refused", result.message
    assert run_state.read_run(project) is not None
    assert _queue_ids(project) == [T1]


def test_rc_only_engine_exit_says_no_exception(tmp_path):
    """F6: rc without a Python exception is not a fabricated traceback."""
    from awf import _engine_liveness as lv

    project = _project(tmp_path)
    engine = lv.EngineLiveness(project, {}, interval=0.05).start()
    engine.finish(1)
    records = list((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert records
    assert "(no exception)" in records[0].read_text(encoding="utf-8")


def test_supervise_does_not_count_a_noop_launch_as_a_restart(tmp_path, monkeypatch):
    """F7: a no-op launch is not a restart and must not burn the budget."""
    from awf import pipeline_state, run_state
    from awf.api import pipeline, supervise

    project = _project(tmp_path)
    _write_todo(project, T1)
    run_state.write_run(
        project,
        active=True,
        queue=[{"todo_id": T1, "pipeline": ""}],
        index=0,
        current=T1,
        generation=1,
    )
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        pipeline, "continue_pipeline",
        lambda *a, **k: calls.append(k) or {"run_mode": "noop"},
    )
    result = supervise.supervise_run(
        project, max_restarts=2, pause_seconds=0, iterations=3,
        sleep=lambda _s: None,
    )
    assert calls
    assert result["restarts"] == 0
    assert "keeps dying" not in result["message"]
    log = (project / ".agentic" / "logs" / "awf-supervise.log").read_text()
    assert "nothing to restart" in log


def test_status_does_not_tell_a_closed_run_to_resume_its_engine(tmp_path):
    """F8: the A.6/Q2 invariant on the general ``awf_status`` surface."""
    from awf import api, pipeline_state
    from awf.brief import next_action_from_status

    project = _project(tmp_path)
    _write_todo(project, T1)
    _run_with_queue(project, [T1])
    api.run_finish(project, reason="done")
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    advice = next_action_from_status(api.get_status(project))
    assert "awf_continue" not in advice
    assert "start a new run" in advice.lower()


# ─── (P0b/G2) supervise: an engine that dies mid-launch is budgeted ─────


def test_supervise_counts_a_failed_launch_as_a_restart(tmp_path, monkeypatch):
    """G2: ``run_mode="error"`` (the engine died at once) burned no budget
    and looped forever — the exact crash-loop P0-2 must bound."""
    from awf import pipeline_state, run_state
    from awf.api import pipeline, supervise

    project = _project(tmp_path)
    _write_todo(project, T1)
    run_state.write_run(
        project,
        active=True,
        queue=[{"todo_id": T1, "pipeline": ""}],
        index=0,
        current=T1,
        generation=1,
    )
    pipeline_state.write_state(
        project, stage_name="agent-implementer", todo_id=T1,
        pipeline_pid=_dead_pid(),
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        pipeline, "continue_pipeline",
        lambda *a, **k: calls.append(k) or {"run_mode": "error"},
    )
    result = supervise.supervise_run(
        project, max_restarts=2, pause_seconds=0, iterations=50,
        sleep=lambda _s: None,
    )
    assert result["action"] == "error"
    assert result["restarts"] == 2
    assert len(calls) == 2
    log = (project / ".agentic" / "logs" / "awf-supervise.log").read_text()
    assert "supervise: restart #1 (error)" in log
    assert "supervise: restart #2 (error)" in log


# ─── (P0c/G3) worker MCP isolation sees the project config ─────────────


def test_isolation_reads_the_project_mcp_config(tmp_path, monkeypatch):
    """G3: the worker runs in the project, so a project-declared server is
    live even when the global config never names it."""
    from awf import _env, xdg

    project = _project(tmp_path)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(
        json.dumps({"mcp": {"heavy": {"enabled": True}}}), encoding="utf-8"
    )
    (project / "opencode.json").write_text(
        json.dumps({"mcp": {"project-heavy": {"enabled": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    env = _env.awf_subprocess_env(
        role="worker", project_dir=project, isolate_mcp=True
    )
    payload = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert payload["mcp"]["heavy"]["enabled"] is False
    assert payload["mcp"]["project-heavy"]["enabled"] is False


def test_unreadable_project_mcp_config_warns_not_fails(tmp_path, monkeypatch):
    """G3: a broken project config is a warning, never a silent skip."""
    from awf import _env, xdg

    project = _project(tmp_path)
    config_file = tmp_path / "opencode.json"
    config_file.write_text(
        json.dumps({"mcp": {"heavy": {"enabled": True}}}), encoding="utf-8"
    )
    (project / "opencode.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(xdg, "opencode_config_file", lambda: config_file)
    with pytest.warns(RuntimeWarning, match="unreadable"):
        env = _env.awf_subprocess_env(
            role="worker", project_dir=project, isolate_mcp=True
        )
    assert "OPENCODE_CONFIG_CONTENT" in env
