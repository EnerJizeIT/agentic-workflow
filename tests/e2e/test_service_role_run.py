"""ORCH M5.3 (TODO-0145): сквозная приёмка служебного run + нормализация при adopt.

Стратегия ORCH-06: «инкрементальная нормализация решает конфликты имени и
пересечения без перезапуска setup-фазы и автоматической перезаписи role .md».
Сквозной сценарий (шим, синтетический git-проект, без сети и живых процессов):

1. Активный основной забег (очередь из 2 юнитов T1/T2), пайплайн НЕ запущен.
2. Служебный run создаёт роль-кандидат (shim-исполнитель пишет declared
   output ``.agentic/roles/draft/auditor.md``); основной ``run.yaml``
   побайто неприкосновенен на всём проходе (инвариант M5.2 — повтор в e2e).
3. Кандидат принят (``adopt_role_draft``) С НОРМАЛИЗАЦИЕЙ: зона ``auditor``
   (verify-семейство) пересекается с живой ролью ``qa`` (verify-семейство) →
   BD-31-addenda обновляются. Проверки:
   - роль доступна (``.agentic/roles/auditor.md`` есть);
   - существующие роли изменены ТОЛЬКО addendum-блоком (тело — байтовый
     префикс, один BD-31-блок, нет дублей);
   - setup-фаза НЕ перезапущена, config и pipeline побайто не тронуты.
4. Исходный забег ПРОДОЛЖАЕТСЯ и ЗАВЕРШАЕТСЯ: T1 → done, T2 → done,
   третий ``run_next`` → «queue exhausted» (забег закрыт, оба юнита в
   ``completed``).

Герметичность: spawn пайплайна — реальный движок в подпроцессе
(``tests/infra/opencode_shim``), таймауты config нулевые. Служебная и
основная фазы требуют разных shim-режимов супервизора, поэтому среда
переключается МЕЖДУ запусками (фоновый ребёнок наследует ``os.environ``
в момент spawn).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from conftest import _git_init

from awf import api, run_state
from awf.api import _liveness

T1, T2 = "TODO-0001", "TODO-0002"
SLUG = "auditor"
CAND = f".agentic/roles/draft/{SLUG}.md"
SVC_TODO = "TODO-0003"  # next free id after T1/T2

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"

CONFIG_YAML = (
    "project:\n"
    "  name: m53-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

# 4-стадийный состав. verify = "next" (без коммита): в run-контексте коммит
# требует bound approval (M2.1, awf_approve(evidence=..., verified_sha=...)),
# а hand-written APPROVE его не несёт. Сценарий — про нормализацию роли и
# непрерывность забега, а не про коммит (тот же приём, что в
# test_orch_composition_scenarios: scenario 3).
DEFAULT_PIPELINE = (
    'name: "default"\n'
    "stages:\n"
    '  - name: "plan"\n'
    '    role: "supervisor"\n'
    '  - name: "agent-implementer"\n'
    '    role: "worker"\n'
    '  - name: "agent-qa-review"\n'
    '    role: "worker"\n'
    '  - name: "verify"\n'
    '    role: "supervisor"\n'
    '    on_approved: "next"\n'
)

# Живые роли. qa (slug "qa" → verify-зона) пересекается по verify-семейству
# с кандидатом auditor (slug "auditor" → verify-зона) — нормализация пишет
# disambiguation в обе. worker (generalist) получает generic addendum.
ROLE_FILES = {
    "supervisor": "# Supervisor\nPlan and verify.\n",
    "worker": "# Worker\nExecute the TODO.\n",
    "qa": "# QA\nVerifies the implementation against the requirements.\n",
}


def _make_project(tmp_path: Path) -> Path:
    """Git-проект: .agentic-скелет + 4-стадийный default.yaml + T1/T2 +
    pre-authorized verify signals (AUD11-03 evidence + BD-8 APPROVE)."""
    proj = tmp_path / "svc-role"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "state"):
        (ag / sub).mkdir(parents=True)
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (ag / "pipelines" / "default.yaml").write_text(DEFAULT_PIPELINE, encoding="utf-8")
    for role, content in ROLE_FILES.items():
        (ag / "roles" / f"{role}.md").write_text(content, encoding="utf-8")
    inbox = ag / "inbox"
    ctx = ag / "context"
    for todo_id in (T1, T2):
        (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
        (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
        # BD-8 + AUD11-03: verify approve в run-режиме gated на evidence.
        (inbox / f"APPROVE-{todo_id}.ready").write_text(
            f"signal: APPROVE\ntask_id: {todo_id}\n", encoding="utf-8"
        )
        (ctx / f"RUN-EVIDENCE-{todo_id}.md").write_text(
            "# evidence\npytest -q -> green (simulated); verdict: approve\n",
            encoding="utf-8",
        )
    return proj


def _shim_base_env(monkeypatch) -> None:
    """Общая shim-среда: репо на PYTHONPATH, shim в PATH, без живых моделей."""
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_MODE", "do-step")
    monkeypatch.setenv("AWF_SHIM_CREATE_DECLARED_OUTPUT", "1")
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)


def _shim_service_env(monkeypatch, tmp_path: Path) -> None:
    """Служебная фаза: DERIVED supervisor-мод (plan→plan-todo, verify→
    approve), план-TODO = служебный юнит. Без AWF_SHIM_SUPERVISOR_MODE."""
    state = tmp_path / "state-svc"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state))
    monkeypatch.setenv("AWF_SHIM_PLAN_TODO_ID", SVC_TODO)
    monkeypatch.delenv("AWF_SHIM_SUPERVISOR_MODE", raising=False)


def _shim_main_env(monkeypatch, tmp_path: Path) -> None:
    """Основная фаза: фиксированный AWF_SHIM_SUPERVISOR_MODE=approve —
    verify пишет свежий ACK, который проходит AUD11-03 гейт (evidence
    pre-created). plan-стадия тоже в approve-мод (как scenario 3)."""
    state = tmp_path / "state-main"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state))
    monkeypatch.setenv("AWF_SHIM_SUPERVISOR_MODE", "approve")


def _wait_until(pred, timeout: float = 120, what: str = "condition") -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.5)
    return False


def _stage_name(proj: Path) -> str:
    from awf.pipeline_state import read_state

    return str((read_state(proj) or {}).get("stage_name") or "")


def _pipeline_alive(proj: Path) -> bool:
    return _liveness.resolve(proj)[0]


def _kill_if_alive(proj: Path) -> None:
    if _pipeline_alive(proj):
        from awf.api import kill_pipeline

        kill_pipeline(proj)


def _main_bytes(proj: Path) -> bytes:
    return run_state.run_file(proj).read_bytes()


def _file(proj: Path, rel: str) -> str:
    return (proj / rel).read_text(encoding="utf-8")


@pytest.mark.timeout(600)
def test_service_role_run_normalizes_adopt_and_main_run_completes(tmp_path, monkeypatch):
    """(2) Сквозной сценарий: активный забег (2 юнита) → служебный run
    создаёт роль (кандидат в draft) → кандидат принят с нормализацией →
    исходный забег продолжается и завершается; роль доступна, существующие
    роли изменены только addendum-блоком, config/pipeline не тронуты."""
    proj = _make_project(tmp_path)
    _shim_base_env(monkeypatch)

    # ── 1. Активный основной забег (2 юнита), пайплайн не запущен. ──────
    api.run_start(proj, queue=[T1, T2])
    main_before_service = _main_bytes(proj)
    qa_before = _file(proj, ".agentic/roles/qa.md")
    worker_before = _file(proj, ".agentic/roles/worker.md")
    config_before = _file(proj, ".agentic/config.yaml")
    pipeline_before = _file(proj, ".agentic/pipelines/default.yaml")

    # ── 2. Служебный run создаёт роль-кандидат. ─────────────────────────
    _shim_service_env(monkeypatch, tmp_path)
    res = api.run_service_start(proj, slug=SLUG, description="Аудит безопасности кода.")
    assert res.action == "started", res.message
    assert res.todo_id == SVC_TODO, f"the service unit must be the next free id: {res.todo_id}"
    try:
        # До verify-стадии: кандидат создан declared output'ом (shim-исполнитель).
        assert _wait_until(lambda: _stage_name(proj) == "verify", what="service verify stage"), (
            f"the service pipeline never reached verify. stage={_stage_name(proj)!r}"
        )
        # ACK сим-супервизора гейтится evidence'ом (AUD11-03) — даёт approve.
        api.run_service_approve(proj, SVC_TODO, evidence="e2e: кандидат проверен (shim); verdict: approve")
        assert _wait_until(lambda: not _pipeline_alive(proj), what="service pipeline exit"), (
            "the service pipeline must complete after the approve"
        )
    finally:
        _kill_if_alive(proj)

    # (в) Кандидат создан declared output'ом, остался в draft-области.
    cand = proj / CAND
    assert cand.is_file(), "the draft candidate (declared output) is missing"
    # Основной run.yaml побайто неприкосновенен на всём служебном проходе.
    assert _main_bytes(proj) == main_before_service, "the main run.yaml must stay byte-identical"

    # Служебный забег закрыт; основной побайто цел и активен.
    fin = api.run_service_finish(proj, reason="кандидат готов к принятию")
    assert fin.action == "stopped", fin.message
    assert _main_bytes(proj) == main_before_service
    assert run_state.read_run(proj).get("active") is True

    # ── 3. Adopt С НОРМАЛИЗАЦИЕЙ. ───────────────────────────────────────
    result = api.adopt_role_draft(proj, SLUG)
    norm = result.normalization
    assert "error" not in norm, f"normalization failed: {norm}"

    # Роль доступна после принятия.
    auditor = proj / ".agentic" / "roles" / f"{SLUG}.md"
    assert auditor.is_file(), "the adopted role must be available in the live roles area"
    assert not cand.exists(), "the draft candidate must be consumed by the adopt"

    # Пересечение зон (qa verify vs auditor verify) обнаружено.
    overlap_pairs = {
        frozenset((o["role_a"], o["role_b"])) for o in norm["overlaps"]
    }
    assert frozenset((SLUG, "qa")) in overlap_pairs, (
        f"the qa/auditor verify-zone overlap must be reported: {norm['overlaps']}"
    )

    # Существующие роли изменены ТОЛЬКО addendum-блоком (тело — префикс,
    # один BD-31-блок). Кандидат тоже получил свой addendum.
    auditor_text = auditor.read_text(encoding="utf-8")
    assert "## BD-31" in auditor_text
    qa_after = _file(proj, ".agentic/roles/qa.md")
    assert qa_after.startswith(qa_before.rstrip()), "qa.md body must be preserved byte-for-byte"
    assert qa_after.count("## BD-31") == 1, "exactly one BD-31 block (idempotent, no dup)"
    assert SLUG in qa_after, "the qa addendum must disambiguate against the new role"
    worker_after = _file(proj, ".agentic/roles/worker.md")
    assert worker_after.startswith(worker_before.rstrip()), "worker.md body preserved"
    assert worker_after.count("## BD-31") == 1

    # (c)+(d): setup-фаза НЕ перезапущена — config и pipeline побайто целы.
    assert _file(proj, ".agentic/config.yaml") == config_before, "config.yaml must be untouched"
    assert _file(proj, ".agentic/pipelines/default.yaml") == pipeline_before, (
        "the active pipeline must be untouched (no setup re-run)"
    )
    # Adopt не трогает состояние забега.
    assert _main_bytes(proj) == main_before_service

    # ── 4. Исходный забег продолжается и завершается. ───────────────────
    _shim_main_env(monkeypatch, tmp_path)
    try:
        launched1 = api.run_next(proj, auto=True)
        assert launched1.action == "started", launched1.message
        assert _wait_until((proj / ".agentic" / "done" / T1).is_dir, what=f"{T1} archived"), (
            f"{T1} did not complete. state={run_state.read_run(proj)}"
        )

        launched2 = api.run_next(proj, auto=True)
        assert launched2.action == "started", launched2.message
        assert _wait_until((proj / ".agentic" / "done" / T2).is_dir, what=f"{T2} archived"), (
            f"{T2} did not complete. state={run_state.read_run(proj)}"
        )

        # Третий вызов: очередь исчерпана — забег завершается.
        stopped = api.run_next(proj, auto=True)
        assert stopped.action == "stopped", stopped.message
        assert "queue exhausted" in stopped.message, stopped.message
    finally:
        _kill_if_alive(proj)

    # Оба юнита обработаны, забег закрыт.
    final = run_state.read_run(proj)
    assert final.get("active") is False, "the run must be closed after the last unit"
    assert set(final.get("completed") or []) == {T1, T2}, (
        f"both units must be credited completed: {final.get('completed')}"
    )
    # Роль осталась доступна после завершения забега.
    assert auditor.is_file()
