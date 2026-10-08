"""ORCH M6.3 (TODO-0148): матрица швов — пары фич, где живут дефекты.

Сегодняшние дефекты жили на стыках фич: id-стадия×контекст (M3×M4),
отчёт M4×M2, generic-вердикты×служебный слот (M5). Владелец просит
покрытие стыков: по одному герметичному кейсу (шим, tmp-проект, без
сети и живых процессов) на пару.

1. память×привязка: активный забег с goal/criteria/decisions — approve
   с привязкой проходит (M2.1), approve без verified_sha — отказ (V-03).
2. привязка×ревизия: применённая ревизия меняет поколение забега —
   «созревший» до ревизии approve (старое поколение) НЕ коммитит,
   дерево не меняется; свежий approve коммитит (нет ложного отказа).
3. stop×снимок×continue: остановка живого юнита (stop_running) →
   ревизия → обычный continue возобновляет юнит СО СВОИМ снимком
   (стадии из снимка, а не из подменённого файла), следующий элемент
   исполняется по отреvised пайплайну.
4. служебный×основной (расширение M5.3): при активном основном
   забеге служебный run ПРОВАЛИВАется (worker заблокирован) — основной
   run.yaml побайто цел на всём проходе, служебный слот закрывается,
   основной забег продолжается и завершается.
5. draft×нормализация: adopt кандидата с пересечением зон — addenda
   обновлены, тела существующих ролей побайто сохранены (хэши до/после),
   роли без пересечения не тронуты вовсе, config/pipeline целы.
6. stage-id×контекст×StageResult: сквозной прогон состава с
   id-стадиями — pipeline-контекст строится без KeyError после
   id-стадии (регресс M4.4), handoff несёт Stage facts
   (stage_id/attempt, M4.3).

Эталон стиля: test_orch_composition_scenarios.py, test_service_role_run.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import _git_init, run_awf

from awf import api, git_utils, run_state
from awf.api import _liveness
from awf.commit_gate import maybe_commit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

T1, T2 = "TODO-0001", "TODO-0002"
ALT = "alt"

CONFIG_YAML = (
    "project:\n"
    "  name: m63-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

# plan -> worker -> verify, verify без коммита: швы 3/4 про
# stop/continue и служебную фазу, не про commit gate (он у швов 1/2).
PIPELINE_NEXT = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)

# Шов 3: подмена default.yaml МЕЖДУ stop и continue — если continue
# возьмёт живой файл, юнит получит стадию, которой у него не было.
PIPELINE_INTRUDER = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "intruder"\n'
    '    role: "auditor"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)

# Шов 3: pipeline, на который ревизируют второй элемент.
PIPELINE_ALT = (
    'name: "alt"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "editor"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)

# Шов 6: повтор роли writer с разными id (draft/revise) — сквозной
# регресс M4.4 (контекст после id-стадии) + M4.3 (Stage facts в handoff).
PIPELINE_ID_STAGES = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "draft"\n'
    '    role: "writer"\n'
    '    task: "Write the first draft"\n'
    '    output: "draft.md"\n'
    '  - id: "revise"\n'
    '    role: "writer"\n'
    '    task: "Revise the draft"\n'
    '    input: "draft.md"\n'
    '    output: "revised.md"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)


# ─── хелперы ──────────────────────────────────────────────────────────────


def _skeleton(proj: Path, roles: dict[str, str], pipeline_yaml: str, todos: tuple) -> None:
    """Throwaway git-проект: .agentic-скелет + pipeline + ready TODO'ы."""
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff", "state"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    for role, content in roles.items():
        (ag / "roles" / f"{role}.md").write_text(content, encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (ag / "pipelines" / "default.yaml").write_text(pipeline_yaml, encoding="utf-8")
    inbox = ag / "inbox"
    for todo_id in todos:
        (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
        (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _make_project(tmp_path: Path, name: str) -> Path:
    proj = tmp_path / name
    proj.mkdir()
    _git_init(proj)
    return proj


def _verify_state(proj: Path, todo_id: str) -> str:
    """Проект в состоянии verify: работа юнита в дереве, baseline на диске.

    Возвращает baseline sha (как в tests/negative/test_approve_binding.py).
    """
    (proj / "unit.txt").write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=proj, check=True)
    baseline_sha = _head(proj)
    (proj / "unit.txt").write_text("v2 good\n", encoding="utf-8")
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{todo_id}.sha").write_text(baseline_sha + "\n", encoding="utf-8")
    return baseline_sha


def _head(proj: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


def _logs(proj: Path) -> Path:
    return proj / ".agentic" / "logs"


def _gate(proj: Path, todo_id: str, baseline_sha: str):
    return maybe_commit(
        "verify", todo_id, "commit_and_next", proj, _logs(proj),
        auto=False, baseline_sha=baseline_sha,
    )


def _assert_tree_intact(proj: Path, baseline_sha: str) -> None:
    assert _head(proj) == baseline_sha, "HEAD moved — a stale approval committed"
    cached = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=proj, capture_output=True
    )
    assert cached.returncode == 0, "unit files were staged"
    assert (proj / "unit.txt").read_text(encoding="utf-8") == "v2 good\n"


def _wait_until(pred, timeout: float = 120, what: str = "condition") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False


def _kill_if_alive(proj: Path) -> None:
    if _liveness.resolve(proj)[0]:
        api.kill_pipeline(proj)


def _main_bytes(proj: Path) -> bytes:
    return run_state.run_file(proj).read_bytes()


def _stage_name(proj: Path) -> str:
    from awf.pipeline_state import read_state

    return str((read_state(proj) or {}).get("stage_name") or "")


def _last_signal(proj: Path) -> str:
    from awf.pipeline_state import read_state

    return str((read_state(proj) or {}).get("last_signal") or "")


def _pipelines_of(state: dict) -> dict:
    return {
        str(q.get("todo_id", "")): str(q.get("pipeline", "") or "")
        for q in state.get("queue") or []
        if isinstance(q, dict)
    }


# ─── шов 1: память×привязка ───────────────────────────────────────────────


def test_seam_memory_binding(tmp_path):
    """(1) Активный забег С ПЛАНОМ (goal/criteria) и причинной памятью:
    approve с привязкой проходит (M2.1: сигнал несёт поколение,
    verified_sha, files_digest) и коммитится; approve БЕЗ verified_sha —
    отказ до публикации (регресс V-03/M2): ни сигнала, ни вердикта,
    план забега на месте."""
    proj = _make_project(tmp_path, "seam1")
    _skeleton(
        proj,
        {"supervisor": "# Supervisor\nPlan and verify.\n", "worker": "# Worker\nExecute.\n"},
        PIPELINE_NEXT,
        (T1,),
    )
    baseline_sha = _verify_state(proj, T1)

    # Забег с планом — «память» шва: goal + criteria + decisions.
    api.run_start(
        proj, queue=[T1], goal="verify the unit", criteria=["unit.txt is v2", "tests green"]
    )
    state = run_state.read_run(proj)
    assert state["generation"] == 1
    assert state["goal"] == "verify the unit"

    # V-03 (регресс): в активном забеге approve без отпечатка — отказ.
    with pytest.raises(api.AwfApiError, match="verified_sha"):
        api.approve_commit(proj, T1, evidence="pytest -q → 1 passed")
    assert not (proj / ".agentic" / "inbox" / f"APPROVE-{T1}.ready").exists()
    state = run_state.read_run(proj)
    assert not state["decisions"], "a refused approve must record no decision"
    assert state["goal"] == "verify the unit", "the run plan must survive the refusal"

    # Счастливый путь того же шва: approve с привязкой проходит.
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(
        proj, T1, evidence="pytest -q → 1 passed; verdict: approve", verified_sha=fp
    )
    binding = json.loads(
        (proj / ".agentic" / "inbox" / f"APPROVE-{T1}.ready").read_text(encoding="utf-8")
    )
    assert binding["generation"] == 1
    assert binding["verified_sha"] == fp
    assert len(binding["files_digest"]) == 64

    # Причинная память: вердикт записан в ТОТ забег, что несёт goal.
    state = run_state.read_run(proj)
    kinds = [d["kind"] for d in state["decisions"]]
    assert "approve" in kinds, f"the approve decision must be in the run diary: {state['decisions']}"
    assert state["goal"] == "verify the unit" and state["criteria"]

    # Привязка работает на гейте: коммит проходит.
    ok = _gate(proj, T1, baseline_sha)
    assert ok.status == "committed", f"the bound approval must commit — {ok.status}: {ok.reason}"
    assert _head(proj) != baseline_sha

    # Память жива после коммита.
    assert run_state.read_run(proj)["decisions"]


# ─── шов 2: привязка×ревизия ─────────────────────────────────────────────


def test_seam_binding_revise_stale_approval_does_not_commit(tmp_path):
    """(2) Ревизия меняет план забега — значит и его цикл: применённая
    ревизия поднимает поколение, и approve, «созревший» ДО ревизии
    (привязан к старому поколению), НЕ разблокирует коммит: гейт
    отказывает, дерево не меняется. Свежий approve (новое поколение)
    коммитит — фикса нет ложного отказа.

    Red before the fix: run_revise не меняет поколение — stale approve
    проходит binding-проверку и гейт коммитит (HEAD сдвигается)."""
    proj = _make_project(tmp_path, "seam2")
    _skeleton(
        proj,
        {"supervisor": "# Supervisor\nPlan and verify.\n", "worker": "# Worker\nExecute.\n"},
        PIPELINE_NEXT,
        (T1, T2),
    )
    baseline_sha = _verify_state(proj, T1)

    api.run_start(proj, queue=[T1, T2])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(
        proj, T1, evidence="pytest -q → 1 passed; verdict: approve", verified_sha=fp
    )
    assert run_state.generation_of(run_state.read_run(proj)) == 1

    # Ревизия: второй элемент переходит на другой пайплайн.
    result = api.run_revise(
        proj, queue=[{"todo_id": T2, "pipeline": ALT}], reason="T2 gets the alt composition",
        key="seam2-k1",
    )
    assert result.action == "applied", result.message
    assert _pipelines_of(run_state.read_run(proj)) == {T1: "", T2: ALT}

    # Stale approve (привязка к поколению 1) коммит не разблокирует —
    # ЗДЕСЬ проявляется дефект до фикса (гейт коммитит: got committed).
    ok = _gate(proj, T1, baseline_sha)
    assert ok.status == "refused", (
        f"a pre-revision approval must not unlock the commit — "
        f"got {ok.status}: {ok.reason}"
    )
    assert "generation" in ok.reason, f"the refusal must name the generation gap: {ok.reason}"
    _assert_tree_intact(proj, baseline_sha)

    # Механика шва: ревизия — новый цикл забега (поколение поднято).
    state = run_state.read_run(proj)
    assert run_state.generation_of(state) == 2, (
        f"an applied revision must move the run to a new cycle "
        f"(generation 2), got {run_state.generation_of(state)}"
    )
    entry = state["revisions"][0]
    assert entry["key"] == "seam2-k1" and entry["generation"] == 1

    # Нет ложного отказа: свежий approve на текущем цикле коммитит.
    api.approve_commit(
        proj, T1, evidence="re-verified after the revision; verdict: approve",
        verified_sha=git_utils.tree_fingerprint(proj),
    )
    ok2 = _gate(proj, T1, baseline_sha)
    assert ok2.status == "committed", f"the fresh approval must commit — {ok2.status}: {ok2.reason}"
    assert _head(proj) != baseline_sha


# ─── шов 3: stop×снимок×continue ─────────────────────────────────────────


@pytest.mark.timeout(300)
def test_seam_stop_snapshot_continue_revised_queue(tmp_path, awf_bin, awf_env, monkeypatch):
    """(3) Стоп живого юнита штатным путём (stop_running) → ревизия
    (в том же вызове) → обычный continue возобновляет юнит СО СВОИМ
    снимком: стадии — из снимка, а не из подменённого между stop и
    continue default.yaml (стадия-взломщик не выполняется, plan не
    переигрывается); ревизия поднимает поколение (M6.3: stale-approve
    шва 2 не действует и на этом пути); второй элемент исполняется по
    отреvised пайплайну (sнимок alt).

    Механизм — за test_run_revise_stop.py (M4.1) + сценарий 3
    test_orch_composition_scenarios.py (M4.4); шов в том, что stop и
    ревизия и снимок работают ВМЕСТЕ в одном забеге."""
    from awf.pipeline_state import read_state

    def _env_stage(worker_mode: str, state_dir: Path, stage_modes: str = "") -> dict:
        env = dict(awf_env)
        env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
        env["AWF_SHIM_MODE"] = worker_mode
        env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
        env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
        env["AWF_SHIM_CREATE_DECLARED_OUTPUT"] = "1"
        if stage_modes:
            env["AWF_SHIM_STAGE_MODES"] = stage_modes
        state_dir.mkdir(parents=True, exist_ok=True)
        env["AWF_SHIM_STATE_DIR"] = str(state_dir)
        return env

    # Фоновый ребёнок наследует os.environ в момент spawn: сначала hang
    # (T1 зависает на implement), потом do-step (continue + T2).
    state1 = tmp_path / "state1"
    state2 = tmp_path / "state2"
    state1.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_MODE", "hang")
    monkeypatch.setenv("AWF_SHIM_SUPERVISOR_MODE", "approve")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state1))
    monkeypatch.setenv("AWF_SHIM_HANG_SECONDS", "60")
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)

    proj = _make_project(tmp_path, "seam3")
    _skeleton(
        proj,
        {
            "supervisor": "# Supervisor\nPlan and verify.\n",
            "worker": "# Worker\nExecute.\n",
            "auditor": "# Auditor\nAudit.\n",
        },
        PIPELINE_NEXT,
        (T1, T2),
    )
    (proj / ".agentic" / "pipelines" / f"{ALT}.yaml").write_text(
        PIPELINE_ALT, encoding="utf-8"
    )
    ctx = proj / ".agentic" / "context"
    for todo_id in (T1, T2):
        (proj / ".agentic" / "inbox" / f"APPROVE-{todo_id}.ready").write_text(
            f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
        )
        # AUD11-03: verify в run-режиме гейтится evidence-файлом.
        (ctx / f"RUN-EVIDENCE-{todo_id}.md").write_text(
            "# evidence\npytest -q → green (simulated); verdict: approve\n",
            encoding="utf-8",
        )

    hang_pids = state1 / "hang-pids"
    try:
        api.run_start(proj, queue=[T1, T2])
        launched = api.run_next(proj, auto=True)
        assert launched.action == "started", launched.message

        assert _wait_until(hang_pids.is_file, what="worker hang stage"), (
            f"the worker never reached the hang stage. state={read_state(proj)!r}"
        )
        assert _liveness.resolve(proj)[0], "the unit must be live at the worker stage"

        # Стоп + ревизия ВМЕСТЕ: второй элемент уходит на alt-состав.
        result = api.run_revise(
            proj, queue=[{"todo_id": T2, "pipeline": ALT}],
            reason="stop the unit; T2 gets the alt composition",
            key="seam3-k1", stop_running=True,
        )
        assert result.action == "applied", result.message
        assert result.resume_from == "implement", (
            f"the resume point must be the stopped stage, got {result.resume_from!r}"
        )

        # Шов M6.3: ревизия подняла поколение (stale-approve шва 2 мёртв).
        state = run_state.read_run(proj)
        assert run_state.generation_of(state) == 2, state
        entry = state["revisions"][0]
        assert entry["key"] == "seam3-k1" and entry["resume_from"] == "implement"
        assert entry["generation"] == 1, "the record keeps the pre-bump generation"
        assert _pipelines_of(state) == {T1: "", T2: ALT}

        # Остановка реальна: пайплайн и worker мертвы.
        assert _liveness.resolve(proj)[0] is False, "the pipeline must be stopped"
        main_pid = int(hang_pids.read_text(encoding="utf-8").splitlines()[0])
        assert _wait_until(
            lambda: not _liveness.probe_alive(main_pid), timeout=10,
            what="worker death",
        ), f"PID {main_pid} (the worker) survived the stop"
    finally:
        _kill_if_alive(proj)

    # Снимок должен победить: подменяем default.yaml на состав со
    # стадией-взломщиком МЕЖДУ stop и continue.
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(
        PIPELINE_INTRUDER, encoding="utf-8"
    )

    # Обычный continue: возобновление со снятой точки, стадии из снимка.
    run2 = run_awf(
        awf_bin, ["continue", "--auto"],
        cwd=proj, env=_env_stage("do-step", state2), input_data=b"", timeout=240,
    )
    out2 = (run2.stdout + run2.stderr).decode(errors="replace")
    assert run2.returncode == 0, f"continue must complete the unit. rc={run2.returncode}\n{out2}"
    assert "Pipeline complete!" in out2, out2
    assert "Stages: plan implement verify" in out2, out2
    assert "intruder" not in out2, "continue took the republished file, not the snapshot"

    inv2_log = state2 / "invocation.log"
    inv2 = [
        line.split() for line in inv2_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ] if inv2_log.is_file() else []
    workers = [c for c in inv2 if c[1] == "worker"]
    plans = [c for c in inv2 if c[1] == "supervisor" and c[3] == "awf-supervisor-plan"]
    assert workers, f"no worker invocation in {inv2}"
    assert all(c[3] == f"awf-worker-{T1}" for c in workers), workers
    assert not plans, f"the plan stage re-ran — the resume is not from the stop point: {inv2}"
    assert (proj / ".agentic" / "done" / T1 / "DONE.md").is_file()

    # Шов ревизии: второй элемент исполняется по alt-составу (sнимок alt),
    # а не по подменённому default.yaml.
    monkeypatch.setenv("AWF_SHIM_MODE", "do-step")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state2))
    launched2 = api.run_next(proj, auto=True)
    assert launched2.action == "started", launched2.message
    try:
        assert _wait_until(
            (proj / ".agentic" / "done" / T2).is_dir, what=f"{T2} archived"
        ), f"{T2} did not complete. state={run_state.read_run(proj)!r}"
    finally:
        _kill_if_alive(proj)

    snap2 = proj / ".agentic" / "context" / f"PIPELINE-{T2}.yaml"
    assert snap2.is_file(), "T2 snapshot missing"
    assert "editor" in snap2.read_text(encoding="utf-8"), "T2 must run the alt composition"
    snap1 = proj / ".agentic" / "context" / f"PIPELINE-{T1}.yaml"
    assert "implement" in snap1.read_text(encoding="utf-8"), "T1 snapshot must keep implement"
    assert "intruder" not in snap1.read_text(encoding="utf-8")

    # Очередь исчерпана: явный run_next закрывает забег (как в M5.3).
    stopped = api.run_next(proj, auto=True)
    assert stopped.action == "stopped", stopped.message
    assert "queue exhausted" in stopped.message, stopped.message
    final = run_state.read_run(proj)
    assert final.get("active") is False, "the run must be closed after the last unit"
    assert set(final.get("completed") or []) == {T1, T2}


# ─── шов 4: служебный×основной (failure-угол, расширение M5.3) ────────────


SVC_TODO = "TODO-0003"  # next free id after T1/T2
SVC_SLUG = "auditor"
SVC_CAND = f".agentic/roles/draft/{SVC_SLUG}.md"


@pytest.mark.timeout(300)
def test_seam_service_failure_main_run_intact(tmp_path, monkeypatch):
    """(4) Активный основной забег — служебный run ПРОВАЛИВАется
    (worker заблокирован: on_blocked: stop, pipeline остановлен).
    Проверка шва: основной ``run.yaml`` побайто цел на КАЖДОМ
    контроле (до/после провала/после закрытия слота), служебный слот
    закрывается с отчётом, кандидат не создан, основной забег
    продолжается и завершается до queue exhausted.

    M5.3 доказал счастливый угол (кандидат создан, adopt, main цел);
    здесь — угол провала: main должен пережить неудачный служебный run
    так же безупречно."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_CREATE_DECLARED_OUTPUT", "1")
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)

    proj = _make_project(tmp_path, "seam4")
    _skeleton(
        proj,
        {"supervisor": "# Supervisor\nPlan and verify.\n", "worker": "# Worker\nExecute.\n"},
        PIPELINE_NEXT,
        (T1, T2),
    )
    ctx = proj / ".agentic" / "context"
    for todo_id in (T1, T2):
        (proj / ".agentic" / "inbox" / f"APPROVE-{todo_id}.ready").write_text(
            f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
        )
        (ctx / f"RUN-EVIDENCE-{todo_id}.md").write_text(
            "# evidence\npytest -q → green (simulated); verdict: approve\n",
            encoding="utf-8",
        )

    # 1. Активный основной забег (2 юнита), пайплайн не запущен.
    api.run_start(proj, queue=[T1, T2])
    main_before = _main_bytes(proj)
    assert not run_state.run_file(proj, slot="service").exists(), (
        "no service slot before the service start"
    )

    # 2. Служебный run: worker write-blocked → on_blocked: stop.
    state_svc = tmp_path / "state-svc"
    state_svc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWF_SHIM_MODE", "write-blocked")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state_svc))
    monkeypatch.setenv("AWF_SHIM_PLAN_TODO_ID", SVC_TODO)
    monkeypatch.delenv("AWF_SHIM_SUPERVISOR_MODE", raising=False)

    res = api.run_service_start(
        proj, slug=SVC_SLUG, description="Role creation — must fail in this seam."
    )
    assert res.action == "started", res.message
    assert res.todo_id == SVC_TODO, res.todo_id
    try:
        # TODO-0167: the .ready is TRANSIENT — the engine consumes it right
        # after classifying the signal (NEG-4: the fired .ready is unlinked,
        # the .md kept as evidence), and the signal watch polls every
        # BD20_POLL_INTERVAL (3s) — the file's lifetime is that remainder
        # plus teardown, and can be near zero. The old 0.5s poll could miss
        # the whole window, then burn 120s on a file that never reappears
        # (the 08.10 CI flake: green locally, red on the runner). The
        # persistent records of the same event: last_signal in
        # state/current.yaml (engine-written BEFORE the consumption) and the
        # kept BLOCKED-*.md — asserting on those keeps "the signal appeared"
        # strict while removing the sampling race.
        blocked_md = proj / ".agentic" / "outbox" / f"BLOCKED-{SVC_TODO}.md"
        assert _wait_until(
            lambda: _last_signal(proj) == f"BLOCKED-{SVC_TODO}" and blocked_md.is_file(),
            what="service block (engine-recorded signal + kept evidence)",
        ), (
            f"the service worker never got blocked. stage={_stage_name(proj)!r} "
            f"last_signal={_last_signal(proj)!r}"
        )
        assert _wait_until(
            lambda: not _liveness.resolve(proj)[0], what="service pipeline exit"
        ), "the blocked service pipeline must stop (on_blocked: stop)"
    finally:
        _kill_if_alive(proj)

    # Провал: main цел (контроль 1), служебный слот открыт (юнит не
    # завершён — закрытие явное), кандидата нет (worker не дошёл до вывода).
    assert _main_bytes(proj) == main_before, "the main run.yaml must survive the service failure"
    svc = run_state.read_run(proj, slot="service")
    assert svc and svc.get("active"), "the blocked service run must stay open until closed"
    assert not (proj / SVC_CAND).exists(), "a blocked worker must not leave a candidate"

    # 3. Закрытие слота: отчёт в outbox, слот неактивен, main не тронут.
    fin = api.run_service_finish(proj, reason="the service unit failed (worker blocked)")
    assert fin.action == "stopped", fin.message
    assert fin.report_file and Path(fin.report_file).is_file(), (
        f"the service run report is missing: {fin.report_file!r}"
    )
    assert _main_bytes(proj) == main_before, "the finish must not write the main run.yaml"
    svc = run_state.read_run(proj, slot="service")
    assert svc.get("active") is False and svc.get("stop_reason"), svc
    assert run_state.read_run(proj).get("active") is True, "the main run must stay active"

    # 4. Основной забег продолжается и завершается.
    state_main = tmp_path / "state-main"
    state_main.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWF_SHIM_MODE", "do-step")
    monkeypatch.setenv("AWF_SHIM_SUPERVISOR_MODE", "approve")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state_main))
    try:
        l1 = api.run_next(proj, auto=True)
        assert l1.action == "started", l1.message
        assert _wait_until((proj / ".agentic" / "done" / T1).is_dir, what=f"{T1} archived"), (
            f"{T1} did not complete. state={run_state.read_run(proj)!r}"
        )
        l2 = api.run_next(proj, auto=True)
        assert l2.action == "started", l2.message
        assert _wait_until((proj / ".agentic" / "done" / T2).is_dir, what=f"{T2} archived"), (
            f"{T2} did not complete. state={run_state.read_run(proj)!r}"
        )
        l3 = api.run_next(proj, auto=True)
        assert l3.action == "stopped" and "queue exhausted" in l3.message, l3.message
    finally:
        _kill_if_alive(proj)

    final = run_state.read_run(proj)
    assert final.get("active") is False, "the run must be closed after the last unit"
    assert set(final.get("completed") or []) == {T1, T2}, final.get("completed")


# ─── шов 5: draft×нормализация ────────────────────────────────────────────


def _body_hash(text: str) -> str:
    """Хэш ТЕЛА роли — файла без BD-31-хвоста.

    Вырезание тем же паттерном, что и в движке (awf/api/roles.py, step 4):
    ``\\n*---\\n\\n## BD-31: Pipeline-specific disambiguation.*$`` + rstrip —
    то есть тело до/после должно совпасть байт в байт."""
    import hashlib
    import re

    tail = re.compile(
        r"\n*---\n\n## BD-31: Pipeline-specific disambiguation.*$", re.DOTALL
    )
    body = tail.sub("", text).rstrip()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_seam_draft_normalization_adopt_preserves_bodies(tmp_path):
    """(5) Adopt кандидата с пересечением зон (auditor×qa — verify-семейство):
    addenda обновлены (BD-31-блок у qa и у принятой роли), ТЕЛА существующих
    ролей побайто сохранены (хэш тела до/после — «не перезаписаны»), тело
    кандидата переехало в живую роль, config и pipeline побайто целы
    (setup-фаза не перезапущена, ORCH M5.3).

    M5.3 доказал этот же шов через служебный run (кандидат — declared
    output); здесь кандидат создаётся напрямую через draft-область (M5.1) —
    нормализация не должна зависеть от того, КТО принёс кандидата."""
    proj = _make_project(tmp_path, "seam5")
    _skeleton(
        proj,
        {
            "supervisor": "# Supervisor\nPlan and verify.\n",
            "worker": "# Worker\nExecute.\n",
            "qa": "# QA\nVerifies the implementation against the requirements.\n",
        },
        PIPELINE_NEXT,
        (),
    )
    roles_dir = proj / ".agentic" / "roles"
    qa_before = (roles_dir / "qa.md").read_text(encoding="utf-8")
    worker_before = (roles_dir / "worker.md").read_text(encoding="utf-8")
    qa_body_h0 = _body_hash(qa_before)
    worker_body_h0 = _body_hash(worker_before)
    config_before = (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    pipe_before = (proj / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8")

    # Кандидат в draft-области (M5.1): живой роли "auditor" нет.
    api.add_role(
        proj, "auditor",
        description="Audits code security: input validation, secrets handling.",
        draft=True,
    )
    cand = roles_dir / "draft" / "auditor.md"
    assert cand.is_file(), "the draft candidate is missing"
    assert not (roles_dir / "auditor.md").exists(), "draft must not touch the live area"

    result = api.adopt_role_draft(proj, "auditor")
    norm = result.normalization
    assert "error" not in norm, f"normalization failed: {norm}"
    overlap_pairs = {frozenset((o["role_a"], o["role_b"])) for o in norm["overlaps"]}
    assert frozenset(("auditor", "qa")) in overlap_pairs, norm["overlaps"]

    # Кандидат принят: живая роль есть, draft-кандидат потреблён, тело
    # кандидата (описание) переехало в живую роль.
    live = roles_dir / "auditor.md"
    assert live.is_file() and not cand.exists()
    live_text = live.read_text(encoding="utf-8")
    assert "Audits code security" in live_text
    assert live_text.count("## BD-31") == 1

    # ТЕЛА существующих ролей побайто сохранены; addendum — только блок.
    qa_after = (roles_dir / "qa.md").read_text(encoding="utf-8")
    assert _body_hash(qa_after) == qa_body_h0, "qa.md body was overwritten by the adopt"
    assert qa_after.count("## BD-31") == 1, "exactly one BD-31 block (idempotent)"
    assert "auditor" in qa_after, "the qa addendum must disambiguate against the new role"

    worker_after = (roles_dir / "worker.md").read_text(encoding="utf-8")
    assert _body_hash(worker_after) == worker_body_h0, "worker.md body was overwritten"
    assert worker_after.count("## BD-31") <= 1, "no duplicated BD-31 blocks"

    # Setup-фаза не перезапущена: config и pipeline побайто целы.
    assert (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8") == config_before
    assert (proj / ".agentic" / "pipelines" / "default.yaml").read_text(encoding="utf-8") == pipe_before


# ─── шов 6: stage-id×контекст×StageResult ─────────────────────────────────


@pytest.mark.timeout(180)
def test_seam_id_stages_context_and_stage_facts(tmp_path, awf_bin, awf_env):
    """(6) Сквозной прогон состава с id-стадиями (повтор роли writer:
    draft → revise): pipeline-контекст строится без KeyError на стадии
    ПОСЛЕ id-стадии (регресс M4.4 — прогон завершается), и handoff каждой
    id-стадии несёт Stage facts: stage_id = СВОЙ id экземпляра, attempt
    (M4.3). Роль одна, id разные — факты не должны смешиваться."""
    proj = _make_project(tmp_path, "seam6")
    _skeleton(
        proj,
        {"supervisor": "# Supervisor\nPlan and verify.\n", "writer": "# Writer\nWrite.\n"},
        PIPELINE_ID_STAGES,
        (T1,),
    )
    (proj / ".agentic" / "inbox" / f"APPROVE-{T1}.ready").write_text(
        f"signal: APPROVE\ntask_id: {T1}\n", encoding="utf-8"
    )

    state = tmp_path / "state6"
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = "do-step"
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    env["AWF_SHIM_CREATE_DECLARED_OUTPUT"] = "1"
    state.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state)

    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", T1],
        cwd=proj, env=env, input_data=b"", timeout=150,
    )
    out1 = (run1.stdout + run1.stderr).decode(errors="replace")
    # M4.4-регресс: прогон не упал на стадии после id-стадии.
    assert run1.returncode == 0, f"the id-stage composition must complete. rc={run1.returncode}\n{out1}"
    assert "Pipeline complete!" in out1, out1
    assert "Stages: plan draft revise verify" in out1, out1
    assert "KeyError" not in out1, out1

    # Объявленные выходы созданы (контракт стадий выдержан).
    assert (proj / "draft.md").is_file()
    assert (proj / "revised.md").is_file()

    # Handoff'ы id-стадий — по экземпляру, с СВОИМИ Stage facts.
    hd = proj / ".agentic" / "done" / T1 / "handoff"
    draft_hd = (hd / f"draft-{T1}.md").read_text(encoding="utf-8")
    revise_hd = (hd / f"revise-{T1}.md").read_text(encoding="utf-8")
    for facts in (draft_hd, revise_hd):
        assert "## Stage facts" in facts, f"Stage facts section missing: {facts[:400]!r}"
        assert "- attempt: 1" in facts
    assert "- stage_id: `draft`" in draft_hd, draft_hd
    assert "- stage_id: `revise`" in revise_hd, revise_hd
    assert "- stage_id: `revise`" not in draft_hd, "stage facts must not mix between instances"
    assert "- stage_id: `draft`" not in revise_hd, "stage facts must not mix between instances"
