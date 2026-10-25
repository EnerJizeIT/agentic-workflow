"""TODO-0241: живучесть движка — heartbeat, parent-death, engine-exit, статус, reaper.

Три тихие смерти пайплайна (0238, 0228, 0240): воркер падал, движок
завершался, родитель не забирал rc (зомби ``Zs``), state говорил «стадия
идёт», salvage не писался.

Кейсы:
(a) stub-движок пишет heartbeat и выходит rc=3 → ENGINE-EXIT с rc/stage/todo;
(b) heartbeat: старт, смена стадии, тик;
(c) статус: мёртвый PID → ``engine_dead`` + ``awf_continue``; живой → нет;
(d) reaper: после выхода стаба PID не ``Zs``, rc известен;
(e) не-Linux parent-death — no-op без исключения.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

T = "TODO-0241"
_ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("state", "context", "logs", "inbox", "outbox"):
        (project / ".agentic" / name).mkdir(parents=True, exist_ok=True)
    return project


def _run_python(code: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(_ROOT)}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


# ─── (a): engine-exit на любом завершении ───────────────────────────────


def test_engine_exit_records_rc_stage_and_todo(tmp_path):
    """(a) вложенный движок: start → on_stage → finish(3) → ENGINE-EXIT."""
    project = _project(tmp_path)
    code = f"""
from pathlib import Path
from awf._engine_liveness import EngineLiveness

lv = EngineLiveness(Path(r"{project}"), interval=0.05)
lv.start()
lv.on_stage("worker", "{T}")
lv.finish(3)
"""
    result = _run_python(code, cwd=project)
    assert result.returncode == 0, result.stderr

    exits = sorted((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert exits, "engine-exit record was not written"
    text = exits[-1].read_text(encoding="utf-8")
    assert "- rc: 3" in text
    assert "worker" in text
    assert T in text
    assert "signal: none" in text


# ─── (b): heartbeat — старт / смена стадии / тик ────────────────────────


def test_heartbeat_written_and_refreshed(tmp_path):
    """(b) файл появляется на старте, stage меняется, watchdog тикает."""
    from awf._engine_liveness import EngineLiveness, read_heartbeat

    project = _project(tmp_path)
    lv = EngineLiveness(project, interval=0.05).start()
    try:
        first = read_heartbeat(project)
        assert first is not None, "heartbeat missing after start"
        assert first["pid"] == os.getpid()

        lv.on_stage("agent-implementer", T)
        staged = read_heartbeat(project)
        assert staged["stage"] == "agent-implementer"
        assert staged["todo_id"] == T

        stamp = staged["ts_epoch"]
        deadline = time.time() + 2.0
        while time.time() < deadline:
            time.sleep(0.06)
            ticked = read_heartbeat(project)
            if ticked and ticked["ts_epoch"] > stamp:
                break
        else:  # pragma: no cover - diagnostics
            pytest.fail("heartbeat was not refreshed by the watchdog")
    finally:
        lv.finish(0)


# ─── (c): статус не врёт ────────────────────────────────────────────────


def _write_state(project: Path, pid: int) -> None:
    from awf.pipeline_state import write_state

    write_state(
        project,
        stage_idx=1,
        stage_name="agent-implementer",
        stage_kind="execute",
        todo_id=T,
        pipeline_pid=pid,
    )


def test_status_reports_engine_dead_on_dead_pid(tmp_path):
    """(c) state «стадия идёт», pid мёртв → engine_dead + awf_continue."""
    from awf.api import get_status
    from awf.brief import next_action_from_status

    project = _project(tmp_path)
    _write_state(project, 2**31)  # outside the OS pid range — cannot be alive

    status = get_status(project)
    assert status.engine_dead, "status did not report a dead engine"
    assert "stage=agent-implementer" in status.engine_dead
    assert f"todo={T}" in status.engine_dead
    assert status.engine_dead_todo == T
    assert "awf_continue" in next_action_from_status(status)

    from awf._engine_liveness import engine_dead_hint

    expected = engine_dead_hint(status.engine_dead, status.engine_dead_todo)
    # REVIEW D1: the status carries the FACT; the advice order belongs to
    # next_action_from_status alone, so no prefilled expected_action.
    assert status.expected_action is None
    assert next_action_from_status(status) == expected


def test_three_surfaces_agree_on_the_dead_engine_advice(tmp_path):
    """REVIEW D1: status/brief/run_status return the same resume sentence."""
    import asyncio
    import sys

    from awf import api
    from awf._engine_liveness import engine_dead_hint
    from awf.api import get_status
    from awf.brief import next_action_from_status

    plugin_src = _ROOT / "agent_workflow_ui" / "src"
    if str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: liveness\n", encoding="utf-8"
    )
    api.run_start(project, queue=[T], budget_minutes=60)
    _write_state(project, 2**31)

    from agent_workflow_ui.tools import awf as awf_tools

    status = get_status(project)
    assert status.engine_dead
    expected = engine_dead_hint(status.engine_dead, status.engine_dead_todo or "")

    assert next_action_from_status(status) == expected
    assert api.brief(project).next_action == expected
    run = asyncio.run(awf_tools.awf_run_status(project_dir=str(project)))
    assert run.get("status") == "ok", run
    assert run.get("next_action") == expected


def test_three_surfaces_agree_on_the_blocked_advice(tmp_path):
    """REVIEW E3: BLOCKED + dead pid on the first unit — one advice everywhere.

    The state is real and permanent: a BLOCKED stop keeps the closure in
    ``outbox/`` and leaves the stage state (with the now dead pid) on disk.
    A configured project whose FIRST unit crashed has no ``done/`` and no
    run record, so the brief must not fall back to the setup chain. All
    three surfaces answer the same sentence — read the BLOCKED report,
    answer, then ACK (blocked above the engine-dead resume).
    """
    import asyncio
    import sys

    from awf import api
    from awf.api import get_status
    from awf.brief import next_action_from_status

    plugin_src = _ROOT / "agent_workflow_ui" / "src"
    if str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: liveness\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.md").write_text(
        f"# {T}\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text("", encoding="utf-8")
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.md").write_text(
        "# blocked: the worker asks a question\n", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    _write_state(project, 2**31)

    from agent_workflow_ui.tools import awf as awf_tools

    status = get_status(project)
    assert status.blocked_ids == [T]
    assert status.engine_dead
    expected = next_action_from_status(status)
    assert "BLOCKED" in expected and "ack=" in expected, expected

    assert api.brief(project).next_action == expected
    run = asyncio.run(awf_tools.awf_run_status(project_dir=str(project)))
    assert run.get("status") == "ok", run
    assert run.get("next_action") == expected


def test_status_does_not_contradict_a_live_pipeline(tmp_path, monkeypatch):
    """REVIEW C1: launch window — live pid file wins over stale state pid.

    ``start_in_background`` writes the pid file at spawn, while
    ``current.yaml`` is rewritten only at the first stage. Status must not
    report BOTH ``pipeline_running`` and ``engine_dead`` (the resolver owns
    the liveness fact) and must not advise ``awf_continue`` on a live run.
    """
    from awf.api import _liveness, get_status
    from awf.brief import next_action_from_status

    project = _project(tmp_path)
    _write_state(project, 2**31)
    live_pid = os.getpid()
    (project / ".agentic" / "logs" / "awf-start.pid").write_text(
        f"{live_pid}\n", encoding="utf-8"
    )
    fake_cmdline = f"python -m awf start --project-dir {project}"
    real_read_cmdline = _liveness.read_cmdline
    monkeypatch.setattr(
        _liveness,
        "read_cmdline",
        lambda pid: fake_cmdline if pid == live_pid else real_read_cmdline(pid),
    )

    status = get_status(project)
    assert status.pipeline_running is True
    assert not status.engine_dead
    assert "awf_continue" not in next_action_from_status(status)


def test_awf_run_status_resumes_a_dead_engine(tmp_path):
    """REVIEW C2: ``awf_run_status.next_action`` = awf_continue on death."""
    import asyncio
    import sys

    from awf import api

    plugin_src = _ROOT / "agent_workflow_ui" / "src"
    if str(plugin_src) not in sys.path:
        sys.path.insert(0, str(plugin_src))

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: liveness\n", encoding="utf-8"
    )
    api.run_start(project, queue=[T], budget_minutes=60)
    _write_state(project, 2**31)

    from agent_workflow_ui.tools import awf as awf_tools

    result = asyncio.run(awf_tools.awf_run_status(project_dir=str(project)))
    assert result.get("status") == "ok", result
    assert result.get("engine_dead")
    assert f"awf_continue(todo_id='{T}')" in result.get("next_action", "")

    from awf._engine_liveness import engine_dead_hint

    assert result.get("next_action") == engine_dead_hint(
        result["engine_dead"], result.get("engine_dead_todo") or ""
    )


def test_status_silent_on_live_pid(tmp_path):
    """(c) живой PID + свежий heartbeat → engine_dead нет."""
    from awf._engine_liveness import write_heartbeat
    from awf.api import get_status

    project = _project(tmp_path)
    _write_state(project, os.getpid())
    write_heartbeat(project, stage="agent-implementer", todo_id=T)
    status = get_status(project)
    assert not status.engine_dead


def test_status_heartbeat_stale_marks_engine_dead(tmp_path):
    """(c) PID жив, heartbeat ЭТОГО pid старше порога → engine_dead."""
    from awf._engine_liveness import write_heartbeat
    from awf.api import get_status

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "engine:\n  heartbeat_stale_seconds: 1\n", encoding="utf-8"
    )
    _write_state(project, os.getpid())
    write_heartbeat(project, stage="agent-implementer", todo_id=T)
    time.sleep(1.2)
    status = get_status(project)
    assert status.engine_dead
    assert "heartbeat stale" in status.engine_dead


def test_foreign_heartbeat_is_not_death_evidence(tmp_path):
    """(c) heartbeat чужого pid не судит о живом pid state: не death."""
    from awf._engine_liveness import write_heartbeat
    from awf.api import get_status

    project = _project(tmp_path)
    (project / ".agentic" / "config.yaml").write_text(
        "engine:\n  heartbeat_stale_seconds: 1\n", encoding="utf-8"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _write_state(project, proc.pid)
        write_heartbeat(project, stage="agent-implementer", todo_id=T)
        time.sleep(1.2)
        status = get_status(project)
        assert not status.engine_dead
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_salvage_owns_its_surface_on_both_statuses(tmp_path):
    """ARCH: pending salvage suppresses ``engine_dead`` on BOTH surfaces.

    The worker-death advice ("review the diff / ACK / retry") must not be
    replaced by "resume the engine" when the engine itself died while the
    salvage flag was still on disk. The guard lives in one owner
    (``status_engine_dead``), not next to each caller.
    """
    from awf._engine_liveness import status_engine_dead
    from awf.api import get_status, run_status
    from awf.pipeline_state import write_state

    project = _project(tmp_path)
    write_state(
        project,
        stage_idx=1,
        stage_name="agent-implementer",
        stage_kind="execute",
        todo_id=T,
        pipeline_pid=2**31,
        salvage_needed=True,
        salvage_stage="agent-implementer",
    )

    assert status_engine_dead(
        project,
        {
            "stage_name": "agent-implementer",
            "todo_id": T,
            "pipeline_pid": 2**31,
            "salvage_needed": True,
        },
        None,
        pipeline_running=False,
    ) is None

    assert not get_status(project).engine_dead
    assert not run_status(project).engine_dead


# ─── (d): reaper забирает rc, зомби не остаётся ─────────────────────────


def test_reaper_collects_rc_and_leaves_no_zombie(tmp_path):
    """(d) после выхода стаба PID переиспользован/убран, rc=3 известен."""
    from awf.api._background import _reap_in_background

    project = _project(tmp_path)
    logs = project / ".agentic" / "logs"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    thread = _reap_in_background(proc, logs)
    assert thread is not None
    thread.join(timeout=10)
    assert not thread.is_alive(), "reaper thread did not finish"
    assert proc.returncode == 3, "rc was not collected"

    stat = Path(f"/proc/{proc.pid}/stat")
    if stat.exists():
        fields = stat.read_text(encoding="utf-8").split()
        assert fields[2] != "Z", "the stopped engine is still a zombie"

    log = logs / "orchestrator.log"
    assert log.is_file()
    assert "rc=3" in log.read_text(encoding="utf-8")


# ─── (e): не-Linux — явный no-op ────────────────────────────────────────


def test_parent_death_is_noop_off_linux(tmp_path):
    """(e) не-Linux: no-op, одна строка в лог, без исключения."""
    from awf._engine_liveness import install_parent_death_signal

    project = _project(tmp_path)
    logs = project / ".agentic" / "logs"

    assert install_parent_death_signal(logs, platform="darwin-test-0241") is False

    text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "parent-death" in text
    assert "no-op" in text


# ─── (f): остальные пути завершения — сигнал и исключение ────────────────


def _spawn_liveness(project: Path):
    """Subprocess: start the liveness record, print READY, then wait."""
    code = f"""
import time
from pathlib import Path
from awf._engine_liveness import EngineLiveness

lv = EngineLiveness(Path(r"{project}"), interval=0.05)
lv.start()
lv.on_stage("worker", "{T}")
print("READY", flush=True)
time.sleep(60)
"""
    env = {k: v for k, v in os.environ.items() if k != "AWF_BACKGROUND_CHILD"}
    env["PYTHONPATH"] = str(_ROOT)
    return subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(project),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_sigterm_records_signal_and_stage(tmp_path):
    """(f) SIGTERM → ENGINE-EXIT с сигналом, rc=128+sig и стадией."""
    import signal as _signal

    project = _project(tmp_path)
    proc = _spawn_liveness(project)
    try:
        line = proc.stdout.readline().strip()
        assert line == "READY", proc.stdout.read()
        proc.send_signal(_signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover - diagnostics
            proc.kill()
            proc.communicate(timeout=10)

    assert proc.returncode == -_signal.SIGTERM, proc.returncode
    exits = sorted((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert exits, "engine-exit record was not written on SIGTERM"
    text = exits[-1].read_text(encoding="utf-8")
    assert f"- signal: {int(_signal.SIGTERM)}" in text
    assert f"- rc: {128 + int(_signal.SIGTERM)}" in text
    assert "- stage: worker" in text
    assert f"- todo: {T}" in text


def test_exception_tail_is_recorded(tmp_path):
    """(f) исключение → ENGINE-EXIT с rc=1 и хвостом трейсбека."""
    project = _project(tmp_path)
    code = f"""
from pathlib import Path
from awf._engine_liveness import EngineLiveness

lv = EngineLiveness(Path(r"{project}"), interval=0.05)
lv.start()
lv.on_stage("worker", "{T}")
try:
    1 / 0
except ZeroDivisionError:
    lv.note_exception()
lv.finish(1)
"""
    result = _run_python(code, cwd=project)
    assert result.returncode == 0, result.stderr

    exits = sorted((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))
    assert exits, "engine-exit record was not written on the exception path"
    text = exits[-1].read_text(encoding="utf-8")
    assert "- rc: 1" in text
    assert "ZeroDivisionError" in text


def test_never_started_leaves_no_artifacts(tmp_path):
    """(f) отказ до старта (валидация пайплайна) не оставляет следов."""
    from awf._engine_liveness import EngineLiveness, heartbeat_path

    project = _project(tmp_path)
    liveness = EngineLiveness(project, interval=0.05)
    liveness.finish(1, reason="refused launch")

    assert not heartbeat_path(project).exists()
    assert not list((project / ".agentic" / "context").glob("ENGINE-EXIT-*.md"))


# ─── REVIEW QA (vitok 5): reaper на РЕАЛЬНОЙ точке спавна ───────────────

def _stub_child_popen(real_popen, rc: int, captured: dict):
    """``subprocess.Popen`` stand-in: spawn a stub engine that exits with *rc*.

    The real ``start_in_background`` wiring runs (env, argv, pid file,
    reaper thread); only the engine command is swapped for a short-lived
    python stub. The returned object is a real ``Popen`` so ``wait()`` is
    the production call.
    """

    def fake_popen(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = kwargs.get("env")
        proc = real_popen(
            [sys.executable, "-c", f"import sys; sys.exit({rc})"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        captured["pid"] = proc.pid
        return proc

    return fake_popen


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def _pid_state(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return "gone"
    return stat.read_text(encoding="utf-8").split()[2]


def test_background_launch_reaps_the_child_and_logs_rc(tmp_path, monkeypatch):
    """Requirement 5 at the CALL SITE: ``start_in_background`` owns the child.

    The contract test drives ``_reap_in_background`` directly, so deleting
    the call in ``start_in_background`` would leave the suite green while a
    finished engine stayed ``Zs`` and its rc was lost — the very incident.
    Here the production wiring runs with a stub child: the rc must land in
    the log and the pid must not stay a zombie.
    """
    import awf.api._background as bg

    project = _project(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        bg.subprocess, "Popen", _stub_child_popen(subprocess.Popen, 3, captured)
    )

    pid, _log_file, pid_file = bg.start_in_background(
        project, pipeline=None, from_stage=None, auto=False, timeout=30
    )
    assert pid == captured["pid"]
    assert pid_file.is_file(), "the real wiring did not write the PID file"

    log = project / ".agentic" / "logs" / "orchestrator.log"
    assert _wait_for(lambda: log.is_file() and "rc=3" in log.read_text(
        encoding="utf-8"
    )), "the spawned engine's rc never reached the log — nobody reaped it"
    assert "background engine exited: rc=3" in log.read_text(encoding="utf-8")
    assert _pid_state(pid) != "Z", "the finished engine is still a zombie"


def test_reaper_owns_the_child_when_the_pid_file_write_is_refused(
    tmp_path, monkeypatch
):
    """REVIEW A4: the reaper starts AT THE SPAWN, not on the happy return.

    A refused PID-file write still leaves a live child. Before A4 that
    child had no owner: it finished as ``Zs`` and its rc was lost. The
    refusal must not undo the ownership.
    """
    import awf.api._background as bg
    from awf.api._errors import AwfApiError
    from awf.infra import managed_fs

    project = _project(tmp_path)
    captured: dict = {}
    monkeypatch.setattr(
        bg.subprocess, "Popen", _stub_child_popen(subprocess.Popen, 3, captured)
    )
    real_write = managed_fs.managed_write_text

    def refuse_pid_file(path, content, **kwargs):
        if Path(path).name == "awf-start.pid":
            raise managed_fs.ManagedPathError("refused by the QA test")
        return real_write(path, content, **kwargs)

    monkeypatch.setattr(managed_fs, "managed_write_text", refuse_pid_file)

    with pytest.raises(AwfApiError):
        bg.start_in_background(
            project, pipeline=None, from_stage=None, auto=False, timeout=30
        )

    pid = captured["pid"]
    log = project / ".agentic" / "logs" / "orchestrator.log"
    assert _wait_for(lambda: log.is_file() and f"pid={pid}" in log.read_text(
        encoding="utf-8"
    )), "the refused launcher left its child unowned (rc never collected)"
    assert "background engine exited: rc=3" in log.read_text(encoding="utf-8")
    assert _pid_state(pid) != "Z", "the unowned engine stayed a zombie"
