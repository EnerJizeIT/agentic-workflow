"""ORCH M4.1 (TODO-0139, остаток ORCH-02): остановка живого юнита + ревизия.

v1 ``awf_run_revise`` отказывается, пока выполняется стадия («stop the
unit first, ORCH M3.5»). Эту юнит добавляет параметр
``stop_running=true``: вызывающий разрешает остановку живого юнита,
остановка идёт ШТАТНЫМ kill-путём движка (``kill_pipeline``: TERM →
grace → KILL пайплайна, kill дерева воркера, last-kill запись), и
ревизия применяется только после того, как остановка подтверждена
(живость перепроверяется).

Инварианты (TODO-0139):
1. ``stop_running=true`` — опция: без неё v1-отказ на месте.
2. Остановка штатным путём; точка возобновления (state ``stage_name`` /
   ``todo_id``) снимается ДО kill (kill очищает state) и восстанавливается
   после — обычный ``awf continue`` возобновляет юнит со остановленной
   стадии; ответ называет её явно (``resume_from``).
3. Неудачная остановка (процесс пережил kill) → ревизия НЕ применяется;
   run-состояние консистентно, повтор возможен.
4. Живой стадии нет → ``stop_running=true`` не ошибка: no-op остановки,
   ревизия применяется.
5. Запись ревизии в state/run.yaml несёт точку возобновления;
   CAS-семантика поколения — как в v1.

prove_red: сквозной shim-сценарий — запуск многостадийного юнита →
стоп на середине → revise → continue → завершение (снимок pipeline
M3.3 уважается).
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import _git_init, run_awf

from awf import api, run_state
from awf.api import _liveness

T1, T2, T3 = "TODO-0001", "TODO-0002", "TODO-0003"
ALT = "alt"

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

CONFIG_YAML = (
    "project:\n"
    "  name: m41-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

# The unit's own definition: plan -> implement (worker) -> verify.
PIPELINE_A = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)

# Republished between the stop and the continue: the worker stage is
# renamed to "intruder" and runs a different role. If continue resumed
# from this file, the unit would run a stage it never had.
PIPELINE_B = (
    'name: "default"\n'
    'stages:\n'
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "intruder"\n'
    '    role: "auditor"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
)


def _make_project(tmp_path: Path) -> Path:
    """Throwaway git project: .agentic skeleton + pipeline + 3 ready TODOs."""
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "state"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    (ag / "pipelines" / "default.yaml").write_text(PIPELINE_A, encoding="utf-8")
    (ag / "roles" / "worker.md").write_text("# Worker\nExecute the TODO.\n", encoding="utf-8")
    (ag / "roles" / "supervisor.md").write_text("# Supervisor\nPlan and verify.\n", encoding="utf-8")
    # The republished template names role "auditor" — without the file the
    # engine's role check prints a warning that mentions the intruder stage.
    (ag / "roles" / "auditor.md").write_text("# Auditor\nAudit.\n", encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    inbox = ag / "inbox"
    for todo_id in (T1, T2, T3):
        (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
        (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    return proj


def _env(awf_env: dict, worker_mode: str, state_dir: Path) -> dict:
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = worker_mode
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    state_dir.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _invocations(state_dir: Path) -> list[list[str]]:
    log = state_dir / "invocation.log"
    if not log.is_file():
        return []
    return [line.split() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _wait_for_file(path: Path, timeout: float = 90) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.2)
    return False


def _wait_for_dead(pid: int, timeout: float = 10) -> bool:
    """True once the pid is gone (pdeathsig delivery is kernel-async)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _liveness.probe_alive(pid):
            return True
        time.sleep(0.2)
    return not _liveness.probe_alive(pid)


def _state_dump(proj: Path) -> str:
    from awf.pipeline_state import read_state

    return repr(read_state(proj))


def _pipelines(state: dict) -> dict[str, str]:
    return {
        str(q.get("todo_id", "")): str(q.get("pipeline", "") or "")
        for q in state.get("queue") or []
        if isinstance(q, dict)
    }


# ─── prove_red: сквозной shim-сценарий ───────────────────────────────────


@pytest.mark.timeout(300)
def test_stop_running_revise_applies_and_resumes(tmp_path, awf_bin, awf_env, monkeypatch):
    """ORCH M4.1.2+3+5: запуск многостадийного юнита (run_next, фоновый
    процесс) → стоп на середине стадии (run_revise(stop_running=true)
    штатным kill-путём) → применение ревизии → обычный ``awf continue``
    возобновляет юнит СО остановленной стадии и доводит до конца; снимок
    pipeline (M3.3) уважается, plan не переигрывается.

    Red before the fix: ``api.run_revise`` does not accept ``stop_running``
    (TypeError)."""
    # The background child (`python -m awf start ...`) inherits the test
    # process' environment (start_in_background copies os.environ) — the
    # shim controls must be there for the child to see them, and PYTHONPATH
    # pins the repo's awf package from any cwd.
    state1 = tmp_path / "state1"
    state2 = tmp_path / "state2"
    state1.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_MODE", "hang")
    monkeypatch.setenv("AWF_SHIM_SUPERVISOR_MODE", "approve")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state1))
    # The hang is bounded: a mid-test failure must not leak a worker
    # outliving the suite, and a mid-test stop never races the bound.
    monkeypatch.setenv("AWF_SHIM_HANG_SECONDS", "60")
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)
    proj = _make_project(tmp_path)

    # Launch the run + the first element (background child, worker hangs).
    api.run_start(proj, queue=[T1, T2, T3])
    launched = api.run_next(proj)
    assert launched.action == "started", launched.message

    hang_pids = state1 / "hang-pids"
    try:
        assert _wait_for_file(hang_pids), (
            f"the worker never reached the hang stage. state={_state_dump(proj)}"
        )
        assert _liveness.resolve(proj)[0], "the unit must be live at the worker stage"

        # The stop + the revision in ONE call.
        result = api.run_revise(
            proj, queue=[{"todo_id": T2, "pipeline": ALT}],
            reason="stop the unit and revise", key="k-stop-1", stop_running=True,
        )
        assert result.action == "applied", result.message
        assert result.resume_from == "implement", (
            f"the resume point must be the stopped stage, got {result.resume_from!r}"
        )
        assert "resume from stage 'implement'" in result.message, result.message

        # Invariant 5: the revision record carries the resume point.
        state = run_state.read_run(proj)
        assert _pipelines(state) == {T1: "", T2: ALT, T3: ""}, "the queue must be revised"
        entry = state["revisions"][0]
        assert entry["kind"] == "revision" and entry["key"] == "k-stop-1"
        assert entry["resume_from"] == "implement", entry

        # The stop is real: the pipeline AND the worker's main process are
        # dead. (The worker's CHILD — `sleep`, the shim's stand-in for
        # opencode's node children — is a pre-existing engine gap: the
        # worker dies by PR_SET_PDEATHSIG the moment the pipeline dies, so
        # kill_pid_tree finds a dead pid and never signals the worker's
        # process group; the child is orphaned and only the hang bound
        # (60s) reaps it. The fix belongs in kill_pipeline's worker
        # collection (pgid capture while the worker is alive) — outside
        # this unit's file boundary. Recorded in the DONE note; QA picks
        # it up.)
        assert _liveness.resolve(proj)[0] is False, "the pipeline must be stopped"
        main_pid = int(hang_pids.read_text(encoding="utf-8").splitlines()[0])
        assert _wait_for_dead(main_pid), f"PID {main_pid} (the worker) survived the stop"
    finally:
        # A mid-test failure must not leak the pipeline/worker.
        if _liveness.resolve(proj)[0]:
            from awf.api import kill_pipeline

            kill_pipeline(proj)

    # Invariant 3 (snapshot): republish the template — continue must keep
    # the unit's own definition, not the foreign one.
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(PIPELINE_B, encoding="utf-8")

    # Run mode gates the verify decision: inside an active run the
    # interactive wait and the auto-mode fallback only accept the approve
    # signal when the supervisor's independent-verification evidence file
    # exists (AUD11-03) — it is written by awf_approve(evidence=...). The
    # shim supervisor cannot do that ritual, so the test leaves the file
    # the way the supervisor would. (The unit's commit policy is the
    # default "next" — no commit — so the M2.1 binding is not exercised
    # here; the gate under test is the stop/resume, not the approve.)
    (proj / ".agentic" / "context" / f"RUN-EVIDENCE-{T1}.md").write_text(
        "# evidence\npytest -q → green (simulated); verdict: approve\n",
        encoding="utf-8",
    )

    # A PLAIN continue (no pin, no from-stage) resumes from the stop point.
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=_env(awf_env, "do-step", state2), input_data=b"", timeout=240,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, f"continue must complete the unit. rc={run2.returncode}\n{out2}"
    assert "Pipeline complete!" in out2, out2
    # The snapshot's stages ran, not the republished ones.
    assert "Stages: plan implement verify" in out2, out2
    assert "intruder" not in out2, out2

    inv2 = _invocations(state2)
    workers = [c for c in inv2 if c[1] == "worker"]
    plans = [c for c in inv2 if c[1] == "supervisor" and c[3] == "awf-supervisor-plan"]
    assert workers, f"no worker invocation in {inv2}"
    assert all(c[3] == f"awf-worker-{T1}" for c in workers), workers
    assert not plans, f"the plan stage re-ran — the resume is not from the stop point: {inv2}"

    # The unit was driven to the end: on completion it is archived —
    # the DONE report is in done/ (the outbox DONE signal is consumed by
    # the archive).
    assert (proj / ".agentic" / "done" / T1 / "DONE.md").is_file()


# ─── инвариант 3: неудачная остановка → без ревизии ──────────────────────


def _project_unit_test(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="RunReviseStop")
    for t in (T1, T2, T3):
        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / f"{t}.md").write_text(f"# {t}\n", encoding="utf-8")
    return tmp_git_repo


def _launch_first(proj: Path, monkeypatch, queue: list[str]) -> None:
    import awf.api.pipeline as api_pipeline

    def fake_start(project_dir, **kw):
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    api.run_start(proj, queue=queue)
    assert api.run_next(proj).action == "started"


def test_stop_running_stop_failure_refuses_without_revision(tmp_git_repo: Path, monkeypatch) -> None:
    """(в) the process survives the kill (the standard kill path reports
    failure) → the revision is NOT applied: the queue and the revision
    memory are untouched, the run stays active and consistent — the retry
    is possible. The stop must go through the engine's kill_pipeline, not
    a home-grown signal. Red before the fix: TypeError (stop_running)."""
    import awf.api.pipeline as api_pipeline

    proj = _project_unit_test(tmp_git_repo)
    _launch_first(proj, monkeypatch, [T1, T2, T3])
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )
    calls: list[Path] = []

    def fake_kill(project_dir):
        calls.append(project_dir)
        return {
            "killed": False, "pid": 4242, "workers": {},
            "message": "Failed to kill PID 4242.",
        }

    monkeypatch.setattr(api_pipeline, "kill_pipeline", fake_kill)

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}],
        reason="why", key="k1", stop_running=True,
    )
    assert result.action == "refused"
    assert "stop" in result.message.lower()
    assert "not applied" in result.message.lower() or "NOT applied" in result.message
    assert calls == [proj], "the stop must go through kill_pipeline"

    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: "", T3: ""}, (
        "a failed stop must not change the queue"
    )
    assert not state.get("revisions"), "a failed stop must not record a revision"
    assert state.get("active") and state.get("current") == T1, (
        "the run must stay consistent (retry possible)"
    )


# ─── инвариант 4: живой стадии нет → no-op остановки + apply ─────────────


def test_stop_running_without_live_stage_applies(tmp_git_repo: Path, monkeypatch) -> None:
    """(г) no live stage → ``stop_running=true`` is not an error: the stop
    is a no-op and the revision applies (the v1 checks stand). Red before
    the fix: TypeError (stop_running)."""
    proj = _project_unit_test(tmp_git_repo)
    _launch_first(proj, monkeypatch, [T1, T2, T3])

    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}],
        reason="why", key="k1", stop_running=True,
    )
    assert result.action == "applied", result.message
    assert result.resume_from == "", "nothing was stopped — no resume point"
    assert "resume from stage" not in result.message, result.message

    state = run_state.read_run(proj)
    assert _pipelines(state) == {T1: "", T2: ALT, T3: ""}
    entry = state["revisions"][0]
    assert entry["kind"] == "revision" and entry["key"] == "k1"
    assert entry["resume_from"] == ""
