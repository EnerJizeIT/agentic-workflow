"""ORCH M4.4 (ORCH-07 приёмка): сквозные сценарии сменяемости составов.

Стратегия §7: постоянный supervisor + сменяемые pipeline на разных составах.
Этот юнит доводит до сквозного прогона (шим, tmp-проект, без сети и живых
процессов) то, что M3.1/M3.2/M3.3/M3.4/M4.1 собрали по частям:

1. Документный состав с повтором роли — одна роль встречается дважды с
   разными id/task/объявленными выходами; полный прогон до коммита.
   Доказано: стадии адресуются по id, выходы создаются и проверяются,
   handoff'ы раздельные (файлы по id).
2. Кодовый состав с повтором роли — тот же сквозной прогон.
3. Ревизия между юнитами — очередь из двух юнитов; после первого
   awf_run_revise меняет pipeline второго (на состав с повтором роли);
   второй исполняется по снимку новой версии; завершённый первый не тронут.

Ретраи: в сценарии 1b два экземпляра одной роли блокируются по разу при
max_retries=1. Если бюджеты делятся по роли, второй экземпляр не хватает
бюджета и прогон останавливается; если бюджеты per-экземпляр (позиция в
пайплайне), прогон завершается. Доказано завершение.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest
from conftest import _git_init, run_awf

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

T1, T2 = "TODO-0001", "TODO-0002"

CONFIG_YAML = (
    "project:\n"
    "  name: m44-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

ROLE_FILES = {
    "supervisor": "# Supervisor\nPlan and verify.\n",
    "writer": "# Writer\nWrite the document stage.\n",
    "editor": "# Editor\nEdit the document.\n",
    "qa": "# QA\nCheck the document.\n",
    "implementer": "# Implementer\nWrite the code.\n",
    "tester": "# Tester\nTest the code.\n",
    "worker": "# Worker\nExecute the TODO.\n",
}

# Сценарий 1: документный состав. Роль writer встречается ДВАЖДЫ (draft и
# revise) с разными id/task/объявленными выходами.
DOC_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "draft"\n'
    '    role: "writer"\n'
    '    task: "Write the first draft"\n'
    '    output: "draft.md"\n'
    "    max_retries: 1\n"
    '  - id: "revise"\n'
    '    role: "writer"\n'
    '    task: "Revise the draft"\n'
    '    input: "draft.md"\n'
    '    output: "revised.md"\n'
    "    max_retries: 1\n"
    '  - name: "editor"\n'
    '    role: "editor"\n'
    '  - name: "qa"\n'
    '    role: "qa"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "commit_and_next"\n'
)

# Сценарий 2: кодовый состав. Роль implementer ДВАЖДЫ (implement и fix).
CODE_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "implement"\n'
    '    role: "implementer"\n'
    '    task: "Implement the feature"\n'
    '    output: "src/main.py"\n'
    "    max_retries: 1\n"
    '  - name: "tester"\n'
    '    role: "tester"\n'
    '  - id: "fix"\n'
    '    role: "implementer"\n'
    '    task: "Fix the failures"\n'
    "    max_retries: 1\n"
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "commit_and_next"\n'
)

# Сценарий 3: default.yaml (простой состав для первого юнита) и doccomp.yaml
# (документный состав с повтором роли — на него ревизируют второй юнит).
# verify = "next" (без коммита): в run-контексте коммит требует bound approval
# (M2.1, awf_approve(evidence=..., verified_sha=...)), а hand-written APPROVE
# его не несёт — тот же приём, что в tests/negative/test_run_revise_stop.py.
# Сценарий про snapshot/revise, коммит не нужен.
DEFAULT_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "implement"\n'
    '    role: "worker"\n'
    '    on_blocked: "stop"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)

DOCCOMP_PIPELINE = (
    DOC_PIPELINE
    .replace('name: "default"', 'name: "doccomp"')
    .replace('"commit_and_next"', '"next"')
)

# Сценарий 1b: компактный состав с повтором роли (writer: draft, revise) для
# сценария ретраев — без editor/qa, только то, что нужно для доказательства.
RETRY_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - id: "draft"\n'
    '    role: "writer"\n'
    '    task: "Write the first draft"\n'
    '    output: "draft.md"\n'
    "    max_retries: 1\n"
    '  - id: "revise"\n'
    '    role: "writer"\n'
    '    task: "Revise the draft"\n'
    '    input: "draft.md"\n'
    '    output: "revised.md"\n'
    "    max_retries: 1\n"
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "commit_and_next"\n'
)


def _skeleton(proj: Path) -> None:
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "handoff", "state"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    for role, content in ROLE_FILES.items():
        (ag / "roles" / f"{role}.md").write_text(content, encoding="utf-8")
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")


def _make_project(tmp_path: Path, pipeline_yaml: str, todo_id: str = "TODO-0001") -> Path:
    """Throwaway git project: .agentic skeleton + one pipeline + one TODO."""
    proj = tmp_path / "scratch"
    proj.mkdir()
    _git_init(proj)
    _skeleton(proj)
    (proj / ".agentic" / "pipelines" / "default.yaml").write_text(pipeline_yaml, encoding="utf-8")
    inbox = proj / ".agentic" / "inbox"
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    # BD-8: pre-authorize the verify commit so --auto does not wait in the gate.
    (inbox / f"APPROVE-{todo_id}.ready").write_text(
        f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
    )
    return proj


def _make_run_project(tmp_path: Path) -> Path:
    """Two-TODO project with default.yaml + doccomp.yaml for the run queue."""
    proj = tmp_path / "runproj"
    proj.mkdir()
    _git_init(proj)
    _skeleton(proj)
    pipelines = proj / ".agentic" / "pipelines"
    (pipelines / "default.yaml").write_text(DEFAULT_PIPELINE, encoding="utf-8")
    (pipelines / "doccomp.yaml").write_text(DOCCOMP_PIPELINE, encoding="utf-8")
    inbox = proj / ".agentic" / "inbox"
    ctx = proj / ".agentic" / "context"
    for todo_id in (T1, T2):
        (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
        (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
        (inbox / f"APPROVE-{todo_id}.ready").write_text(
            f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
        )
        # AUD11-03: inside a run the verify approve is gated on the
        # independent-verification evidence file. The shim supervisor cannot
        # run that ritual, so the test leaves the file the way the supervisor
        # would (same pattern as tests/negative/test_run_revise_stop.py).
        (ctx / f"RUN-EVIDENCE-{todo_id}.md").write_text(
            "# evidence\npytest -q -> green (simulated); verdict: approve\n",
            encoding="utf-8",
        )
    return proj


def _env(awf_env: dict, worker_mode: str, state_dir: Path,
         stage_modes: str = "") -> dict:
    env = dict(awf_env)
    env["PATH"] = f"{SHIM_DIR}:{env['PATH']}"
    env["AWF_SHIM_MODE"] = worker_mode
    env.pop("AWF_TEST_OPENCODE_BEHAVIOR", None)
    env["AWF_SHIM_SUPERVISOR_MODE"] = "approve"
    # The composition stages declare outputs (draft.md, src/main.py, ...); let
    # the shim create them so the engine's exists+fresh check passes. The
    # negative output scenarios keep the default (off) and prove their own
    # invariants.
    env["AWF_SHIM_CREATE_DECLARED_OUTPUT"] = "1"
    if stage_modes:
        env["AWF_SHIM_STAGE_MODES"] = stage_modes
    state_dir.mkdir(parents=True, exist_ok=True)
    env["AWF_SHIM_STATE_DIR"] = str(state_dir)
    return env


def _wait_for_dir(path: Path, timeout: float = 120) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_dir():
            return True
        time.sleep(0.5)
    return False


def _out(run) -> str:
    return (run.stdout + run.stderr).decode(errors="replace")


# ── prove_red (вскрытый дефект): контекст пайплайна с id-стадией ──────────


def test_pipeline_context_id_addressed_stage_no_keyerror(tmp_path):
    """ORCH M4.4 prove_red: _build_pipeline_context списывает before/after
    по эффективному имени стадии (id или name). Стадия, адресованная id БЕЗ
    явного name, дала KeyError: 'name' — сквозной прогон состава с повтором
    роли падал на стадии ПОСЛЕ такой. Красный до фикса (см. DONE)."""
    from awf.pipeline_state import write_state
    from awf.supervisor import _build_pipeline_context

    proj = _make_project(tmp_path, DOC_PIPELINE)
    # Current stage = "editor" (position 3): before it are draft/revise,
    # both id-addressed with no explicit name.
    write_state(proj, stage_name="editor", todo_id="TODO-0001")

    ctx = _build_pipeline_context(proj, "TODO-0001")
    assert "Before you:" in ctx, f"expected the before-list, got: {ctx!r}"
    for name in ("plan", "draft", "revise"):
        assert name in ctx, f"effective name {name!r} missing from context: {ctx!r}"
    assert "After you: qa, verify" in ctx, ctx


# ── сценарий 1: документный состав, повтор роли, полный прогон до коммита ──


@pytest.mark.timeout(600)
def test_document_composition_repeated_role_full_run(tmp_path, awf_bin, awf_env):
    """Одна роль (writer) дважды: draft и revise. Прогон до коммита; стадии
    адресуются по id, выходы создаются и проверяются, handoff'ы раздельные."""
    proj = _make_project(tmp_path, DOC_PIPELINE)
    env = _env(awf_env, "do-step", tmp_path / "state1")

    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=300,
    )
    out1 = _out(run1)
    assert run1.returncode == 0, f"composition run must complete. rc={run1.returncode}\n{out1}"
    assert "Pipeline complete!" in out1, out1

    # Stages are addressed by id: the run announced the ids, in order.
    assert "Stages: plan draft revise editor qa verify" in out1, out1

    # Declared outputs were created by the worker and the run advanced past
    # them (a missing/stale output routes the stage as blocked and stops).
    assert (proj / "draft.md").is_file(), "draft.md (draft stage output) missing"
    assert (proj / "revised.md").is_file(), "revised.md (revise stage output) missing"

    # Handoffs are separate per instance id — not one file overwritten. They
    # archive to done/{id}/handoff/ on completion (DF6-1), so check there.
    hd = proj / ".agentic" / "done" / "TODO-0001" / "handoff"
    for name in ("draft", "revise", "editor", "qa"):
        assert (hd / f"{name}-TODO-0001.md").is_file(), f"handoff {name}-TODO-0001.md missing"
    # Each repeated-role instance's handoff carries its OWN stage id.
    assert "- stage_id: `draft`" in (hd / "draft-TODO-0001.md").read_text(encoding="utf-8")
    assert "- stage_id: `revise`" in (hd / "revise-TODO-0001.md").read_text(encoding="utf-8")

    # The run archived the TODO (verify approve -> commit_and_next -> archive).
    assert (proj / ".agentic" / "done" / "TODO-0001").is_dir(), "TODO not archived to done/"


# ── сценарий 1b: ретраи не делятся между экземплярами одной роли ───────────


@pytest.mark.timeout(600)
def test_repeated_role_retries_not_shared(tmp_path, awf_bin, awf_env):
    """draft и revise (одна роль writer) блокируются по разу, max_retries=1.
    Per-экземпляр бюджеты (позиция в пайплайне) -> оба проходят на ретрае и
    прогон завершается. Были бы бюджеты per-роль -> revise не хватило бы
    бюджета и прогон остановился (rc=1).

    Escalate пере-планирует НОВЫЙ TODO (ре-план в plan-todo создаёт
    TODO-0002), поэтому current_todo смещается на TODO-0002 — архив идёт в
    done/TODO-0002/. Это штатный путь (см. _handle_escalate), а не баг."""
    proj = _make_project(tmp_path, RETRY_PIPELINE)
    # The replan creates TODO-0002; the verify approves it (BD-8 commit gate).
    (proj / ".agentic" / "inbox" / "APPROVE-TODO-0002.ready").write_text(
        "signal: APPROVE\ntask_id: TODO-0002\n", encoding="utf-8"
    )
    env = _env(
        awf_env, "do-step", tmp_path / "state1",
        stage_modes="draft=blocked:1,revise=blocked:1",
    )
    # Let the supervisor modes derive per kind: plan/replan -> plan-todo
    # (the replan re-plans a new TODO), verify -> approve.
    env.pop("AWF_SHIM_SUPERVISOR_MODE", None)
    env["AWF_SHIM_PLAN_TODO_ID"] = "TODO-0002"

    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=300,
    )
    out1 = _out(run1)
    # Both instances blocked once (the escalate/replan/retry path ran).
    assert out1.count("BLOCKED — escalating to supervisor") == 2, out1
    # Per-instance budgets: the run COMPLETES. A shared (per-role) budget
    # would exhaust on the second instance and stop (rc=1).
    assert run1.returncode == 0, f"retries must be per-instance, run must complete. rc={run1.returncode}\n{out1}"
    assert "Pipeline complete!" in out1, out1
    assert "max retries reached" not in out1, out1
    # The escalate re-planned TODO-0002, so the run archived there.
    assert (proj / ".agentic" / "done" / "TODO-0002").is_dir(), "TODO-0002 not archived to done/"


# ── сценарий 2: кодовый состав, повтор роли, полный прогон ─────────────────


@pytest.mark.timeout(600)
def test_code_composition_repeated_role_full_run(tmp_path, awf_bin, awf_env):
    """Роль implementer дважды: implement (объявленный выход src/main.py) и
    fix. Прогон до коммита; выходы создаются и проверяются, handoff'ы по id."""
    proj = _make_project(tmp_path, CODE_PIPELINE)
    env = _env(awf_env, "do-step", tmp_path / "state1")

    run1 = run_awf(
        awf_bin, ["start", "--auto", "--todo", "TODO-0001"],
        cwd=proj, env=env, input_data=b"", timeout=300,
    )
    out1 = _out(run1)
    assert run1.returncode == 0, f"code composition run must complete. rc={run1.returncode}\n{out1}"
    assert "Pipeline complete!" in out1, out1
    assert "Stages: plan implement tester fix verify" in out1, out1

    # The implement stage's declared output was created and the run advanced.
    assert (proj / "src" / "main.py").is_file(), "src/main.py (implement output) missing"

    hd = proj / ".agentic" / "done" / "TODO-0001" / "handoff"
    for name in ("implement", "tester", "fix"):
        assert (hd / f"{name}-TODO-0001.md").is_file(), f"handoff {name}-TODO-0001.md missing"
    assert "- stage_id: `implement`" in (hd / "implement-TODO-0001.md").read_text(encoding="utf-8")
    assert "- stage_id: `fix`" in (hd / "fix-TODO-0001.md").read_text(encoding="utf-8")
    assert (proj / ".agentic" / "done" / "TODO-0001").is_dir(), "TODO not archived to done/"


# ── сценарий 3: ревизия между юнитами — второй исполняет новый состав ──────


@pytest.mark.timeout(600)
def test_revision_between_units_runs_new_composition(tmp_path, awf_bin, awf_env, monkeypatch):
    """Очередь из двух юнитов. После первого awf_run_revise меняет pipeline
    второго на doccomp (состав с повтором роли). Второй исполняется по снимку
    новой версии; завершённый первый не тронут (его снимок — старый состав)."""
    from awf import api, run_state
    from awf.api import _liveness

    # The background child inherits os.environ (start_in_background copies
    # it) — the shim controls must be there, PYTHONPATH pins the repo's awf.
    state1 = tmp_path / "state1"
    state1.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_MODE", "do-step")
    monkeypatch.setenv("AWF_SHIM_SUPERVISOR_MODE", "approve")
    monkeypatch.setenv("AWF_SHIM_CREATE_DECLARED_OUTPUT", "1")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state1))
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)

    proj = _make_run_project(tmp_path)
    try:
        api.run_start(proj, queue=[T1, T2])
        # auto=True: the launched pipeline runs with --auto, so the supervisor
        # stages (plan/verify) auto-spawn the shim (approve) instead of waiting
        # interactively for an external decision signal (BD-30). The verify
        # approve is gated on RUN-EVIDENCE (AUD11-03), pre-created above.
        launched = api.run_next(proj, auto=True)  # background, T1 on default.yaml
        assert launched.action == "started", launched.message

        assert _wait_for_dir(proj / ".agentic" / "done" / T1), f"T1 did not complete. state={run_state.read_run(proj)}"

        # Between units: revise T2's pipeline to the role-repetition composition.
        result = api.run_revise(
            proj, queue=[{"todo_id": T2, "pipeline": "doccomp"}],
            reason="second unit gets the document composition", key="rev-m44",
        )
        assert result.action == "applied", result.message
        state = run_state.read_run(proj)
        assert _pipelines_of(state) == {T1: "", T2: "doccomp"}, state

        launched2 = api.run_next(proj, auto=True)  # background, T2 on doccomp.yaml
        assert launched2.action == "started", launched2.message
        assert _wait_for_dir(proj / ".agentic" / "done" / T2), f"T2 did not complete. state={run_state.read_run(proj)}"
    finally:
        if _liveness.resolve(proj)[0]:
            from awf.api import kill_pipeline

            kill_pipeline(proj)

    # T2 ran the role-repetition composition — its snapshot has the repeated
    # role (draft/revise), captured from doccomp.yaml at launch.
    snap2 = proj / ".agentic" / "context" / f"PIPELINE-{T2}.yaml"
    assert snap2.is_file(), f"T2 snapshot missing (expected {snap2})"
    snap2_text = snap2.read_text(encoding="utf-8")
    assert "draft" in snap2_text and "revise" in snap2_text, snap2_text

    # T1 ran the OLD (default) composition — its snapshot has no draft/revise.
    snap1 = proj / ".agentic" / "context" / f"PIPELINE-{T1}.yaml"
    assert snap1.is_file(), f"T1 snapshot missing (expected {snap1})"
    snap1_text = snap1.read_text(encoding="utf-8")
    assert "draft" not in snap1_text and "revise" not in snap1_text, snap1_text

    # Both units completed (archived) — the completed first is untouched.
    assert (proj / ".agentic" / "done" / T1 / "DONE.md").is_file()
    assert (proj / ".agentic" / "done" / T2 / "DONE.md").is_file()


def _pipelines_of(state: dict) -> dict:
    return {
        str(q.get("todo_id", "")): str(q.get("pipeline", "") or "")
        for q in state.get("queue") or []
        if isinstance(q, dict)
    }
