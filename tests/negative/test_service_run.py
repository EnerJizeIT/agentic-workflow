"""ORCH M5.2: служебный run создания роли — основной забег неприкосновенен.

Служебный забег — СВОЙ забег в ОТДЕЛЬНОМ файле состояния
(``state/service-run.yaml``): очередь из одного юнита («создай роль
<slug> по описанию …»), запускается тем же движком/пайплайном (снимок
активного состава), а основной ``state/run.yaml`` не перезаписывается и не
завершается — после завершения/провала служебного он остаётся как был
(побайтный пин). Юнит объявляет declared output = путь кандидата
(``.agentic/roles/draft/<slug>.md``, ORCH M3.2: движок проверяет
существование и свежесть), verify НЕ коммитит (кандидат остаётся в
draft-области — adopt отдельный шаг, ORCH M5.1).

Hermetic-часть: spawn пайплайна замещён
(``awf.api.pipeline.start_pipeline``), «живой движок» фейкится на общем
шве (``awf.api._liveness.resolve``) — паттерн test_run_revise.py.
E2E-часть (низ файла) — сквозные shim-сценарии: реальный движок в
подпроцессе (tests/infra/opencode_shim), без сети и живых процессов.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest
from conftest import _git_init

import awf.api.pipeline as api_pipeline
from awf import api, run_state
from awf.api import _liveness
from awf.api._errors import AwfApiError

T1, T2 = "TODO-0001", "TODO-0002"
SLUG = "auditor"
CAND = f".agentic/roles/draft/{SLUG}.md"
PIPE = f"role-draft-{SLUG}"

# Стандартный 4-стадийный состав (как в проекте): plan → implementer →
# qa-review → verify. Роли резолвятся с warning'ом (файлы не обязательны
# для загрузки), но e2e-скелет их создаёт для чистых логов.
DEFAULT_4STAGE = (
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
    '    on_approved: "commit_and_next"\n'
)

CONFIG_YAML = (
    "project:\n"
    "  name: m52-scratch\n"
    "automation:\n"
    "  preflight_timeout_seconds: 0\n"
    "  no_output_timeout_seconds: 0\n"
    "  verify_pack: false\n"
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SHIM_DIR = REPO_ROOT / "tests" / "infra" / "opencode_shim"


# ─── hermetic helpers ────────────────────────────────────────────────────


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="ServiceRun")
    (tmp_git_repo / ".agentic" / "pipelines" / "default.yaml").write_text(
        DEFAULT_4STAGE, encoding="utf-8"
    )
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")


def _fake_start(monkeypatch, proj: Path) -> None:
    def fake_start(project_dir, **kw):
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / ".agentic" / "logs" / "awf-start.out"),
            exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _main_bytes(proj: Path) -> bytes:
    return run_state.run_file(proj).read_bytes()


def _launch_first(proj: Path, monkeypatch, queue: list[str]) -> None:
    """run_start + one launch: index=1, current=queue[0]."""
    _fake_start(monkeypatch, proj)
    api.run_start(proj, queue=queue)
    assert api.run_next(proj).action == "started"


# ─── prove_red: основной run.yaml побайтово неприкосновенен ──────────────


def test_service_run_keeps_main_run_intact(tmp_git_repo: Path, monkeypatch) -> None:
    """(1)+(2) Инвариант M5.2: служебный забег живёт в своём файле
    состояния; основной run.yaml НЕ перезаписывается и НЕ завершается —
    побайто. Вердикт верификации служебного юнита тоже не течёт в журнал
    основного забега.

    Красный до фикса: ``api.run_service_start`` отсутствует (AttributeError)
    — единственный способ пустить «служебный» юнит в активном забеге —
    ``run_start(force=True)``, который ЗАМЕНЯЕТ основную очередь (опасность
    демо в начале теста, она по-прежнему воспроизводится на baseline)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    _fake_start(monkeypatch, proj)
    api.run_start(proj, queue=[T1, T2])
    before = _main_bytes(proj)
    main_before = run_state.read_run(proj)

    # Опасность, которую устраняет юнит (поведение baseline, ещё актуально):
    # «служебный» юнит через run_start(force=True) затирает основной забег.
    api.run_start(proj, queue=["TODO-0901"], force=True)
    assert _main_bytes(proj) != before, (
        "hazard demo: run_start(force=True) rewrites the main run.yaml"
    )
    # Восстанавливаем основной забег для сценария (тестовая подмена файла).
    run_state.run_file(proj).write_bytes(before)

    # Служебный запуск: своя очередь, свой файл состояния.
    res = api.run_service_start(proj, slug=SLUG, description="Аудит безопасности кода.")
    assert res.action == "started", res.message
    svc = run_state.read_run(proj, slot="service")
    assert svc is not None and svc.get("active"), "the service run must be active"
    assert svc["queue"] == [{"todo_id": res.todo_id, "pipeline": PIPE}]
    assert svc["current"] == res.todo_id and svc["index"] == 1

    # Основной run.yaml — побайто как до запуска.
    assert _main_bytes(proj) == before, "the main run.yaml must be byte-identical"
    main = run_state.read_run(proj)
    assert main["queue"] == main_before["queue"]
    assert main["index"] == main_before["index"]
    assert main["current"] == main_before["current"]
    assert main["completed"] == main_before["completed"]
    assert main["decisions"] == main_before["decisions"]
    assert main.get("active") is True

    # Вердикт верификации служебного юнита — в СЛУЖЕБНЫЙ слот, не в журнал
    # основного забега (decisions/outcomes основного не меняются).
    api.run_service_approve(proj, res.todo_id, evidence="проверено: approve")
    assert _main_bytes(proj) == before, (
        "the service verdict must not leak into the main run diary"
    )

    # Закрытие служебного: ответ называет «продолжай основной».
    fin = api.run_service_finish(proj)
    assert fin.action == "stopped", fin.message
    assert "awf_run_next" in fin.next_action and "awf_continue" in fin.next_action
    svc_closed = run_state.read_run(proj, slot="service")
    assert svc_closed is not None and not svc_closed.get("active")
    assert _main_bytes(proj) == before, "the main run must survive the service"
    assert run_state.read_run(proj).get("active") is True


# ─── (а) служебный запуск существует и идёт штатным движком ──────────────


def test_service_launch_uses_generated_pipeline_and_pins_the_todo(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """(а) Служебный запуск есть и идёт тем же движком: start_pipeline
    вызывается с сгенерированным пайплайном и ПРИКОТЫМ служебным TODO
    (порядок очереди, не «самый свежий активный»). Красный до фикса:
    api.run_service_start отсутствует (AttributeError)."""
    calls: list[dict] = []

    def fake_start(project_dir, **kw):
        calls.append(kw)
        return api_pipeline.StartResult(
            run_mode="background", run_id=7, log_file="log",
            exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    api.run_start(proj, queue=[T1, T2])

    res = api.run_service_start(proj, slug=SLUG, description="Описание роли.")
    assert res.action == "started", res.message
    assert calls, "start_pipeline must be called"
    assert calls[0]["pipeline"] == PIPE
    assert calls[0]["todo_id"] == res.todo_id
    assert calls[0]["background"] is True
    # Юнит объявлен в inbox с pipeline в контракт-блоке (RUN3 #2 Part B).
    todo_md = (proj / ".agentic" / "inbox" / f"{res.todo_id}.md").read_text(
        encoding="utf-8"
    )
    assert f"pipeline: {PIPE}" in todo_md


# ─── (в) declared output кандидата в сгенерированном пайплайне ───────────


def test_service_pipeline_declares_candidate_output(tmp_git_repo: Path, monkeypatch) -> None:
    """(3) Сгенерированный служебный пайплайн объявляет кандидата declared
    output стадии-исполнителя (движок проверит exists+fresh, M3.2); verify
    не коммитит (кандидат не коммитится — adopt отдельный шаг, M5.1).
    Проверка свежести движка доказана сценарно в e2e-части файла."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"

    from awf.pipeline import load_stages

    f = proj / ".agentic" / "pipelines" / f"{PIPE}.yaml"
    assert f.is_file(), "the service pipeline file must be generated"
    stages = load_stages(f)
    assert len(stages) == 4, "the standard 4-stage composition is preserved"
    outputs = {s.name: s.output for s in stages}
    assert outputs["agent-implementer"] == CAND
    assert outputs["agent-qa-review"] == ""
    verify = stages[-1]
    assert verify.kind == "verify"
    assert verify.on_approved == "next", "the service run never commits"


def test_engine_freshness_check_rejects_stale_candidate(tmp_git_repo: Path) -> None:
    """(3) Суть M3.2-проверки: кандидат со старым mtime (оставшийся от
    предыдущей попытки) НЕ считается работой стадии — движок назовёт
    «stale». Свежий файл проходит."""
    from awf.pipeline_engine import _declared_output_problem

    proj = _project(tmp_git_repo)
    cand = proj / CAND
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("leftover candidate\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(cand, (past, past))
    now = time.time()
    assert "stale" in _declared_output_problem(proj, CAND, now)
    # Файл, написанный после старта стадии, свежий.
    assert _declared_output_problem(proj, CAND, 0) == ""
    # Отсутствующий файл — тоже не выход.
    cand.unlink()
    assert "does not exist" in _declared_output_problem(proj, CAND, now)


# ─── (г) после служебного — «продолжай основной» ────────────────────────


def test_service_finish_names_main_continuation(tmp_git_repo: Path, monkeypatch) -> None:
    """(4) Закрытие служебного забега: ответ называет продолжение
    основного («awf_run_next / awf_continue») с его текущим снимком;
    репорт в outbox; основной забег побайто цел."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    _launch_first(proj, monkeypatch, [T1, T2])  # index=1, current=T1
    before = _main_bytes(proj)

    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"

    fin = api.run_service_finish(proj, reason="кандидат готов")
    assert fin.action == "stopped", fin.message
    assert "awf_run_next" in fin.next_action and "awf_continue" in fin.next_action
    assert T1 in fin.message, "the main snapshot (current unit) must be shown"
    assert fin.report_file and Path(fin.report_file).is_file()
    report = Path(fin.report_file).read_text(encoding="utf-8")
    assert SLUG in report
    assert _main_bytes(proj) == before


# ─── (д) деградация: служебный упал → основной цел ──────────────────────


def test_service_failure_close_keeps_main_intact(tmp_git_repo: Path, monkeypatch) -> None:
    """(5) Служебный юнит запущен, а пайплайн умер (внешняя гибель
    процесса — salvage-ситуация). Служебный забег закрывается с репортом;
    основной — активен, побайто цел. (Сквозная версия — в e2e-части.)"""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    _launch_first(proj, monkeypatch, [T1, T2])
    before = _main_bytes(proj)

    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"
    # «Смерть» пайплайна — внешнее событие: состояние служебного забега
    # осталось с current=юнит, index сдвинут. Закрытие:
    fin = api.run_service_finish(proj, reason="служебный пайплайн умер (salvage)")
    assert fin.action == "stopped", fin.message
    assert _main_bytes(proj) == before
    main = run_state.read_run(proj)
    assert main.get("active") is True
    assert main["current"] == T1 and main["index"] == 1


# ─── отказы: границы служебного запуска ─────────────────────────────────


def test_service_start_refused_without_active_main_run(tmp_git_repo: Path) -> None:
    """Служебный забег существует, ЧТОБЫ не трогать активный основной;
    без основного — отказ (роль создаётся напрямую: awf_add_role draft).
    Побочных эффектов нет ни одного."""
    proj = _project(tmp_git_repo)
    with pytest.raises(AwfApiError, match="No active main run"):
        api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert run_state.read_run(proj, slot="service") is None
    assert not (proj / ".agentic" / "pipelines" / f"{PIPE}.yaml").exists()
    assert not (proj / ".agentic" / "inbox" / f"{T2}.md").exists()


def test_service_start_refused_while_pipeline_running(tmp_git_repo: Path, monkeypatch) -> None:
    """Один пайплайн на проект (гарантия движка): живой основной юнит —
    отказ ДО любых побочных эффектов (M4.1-семантика: stop first)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    api.run_start(proj, queue=[T1, T2])
    before = _main_bytes(proj)
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "refused", res.message
    assert "running" in res.message and "awf_kill" in res.message
    assert run_state.read_run(proj, slot="service") is None
    assert not (proj / ".agentic" / "pipelines" / f"{PIPE}.yaml").exists()
    # Служебный юнит (TODO-0003) не создан — отказ до побочных эффектов.
    assert not (proj / ".agentic" / "inbox" / "TODO-0003.md").exists()
    assert _main_bytes(proj) == before


def test_service_start_refused_when_service_already_active(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Второй служебный забег поверх активного — отказ (один служебный
    за раз)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    _fake_start(monkeypatch, proj)
    first = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert first.action == "started"
    with pytest.raises(AwfApiError, match="already active"):
        api.run_service_start(proj, slug="other", description="Ещё.")


def test_service_start_rolls_back_on_launch_failure(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """V-01/V-02: провал запуска откатывает ВСЕ побочные эффекты вызова
    (пайплайн-файл, TODO, служебное состояние) — и основной забег
    побайто цел."""
    def bad_start(project_dir, **kw):
        return api_pipeline.StartResult(
            run_mode="error", run_id=None, log_file=None,
            exit_code=1, message="spawn failed",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", bad_start)
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    api.run_start(proj, queue=[T1, T2])
    before = _main_bytes(proj)

    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "refused", res.message
    assert run_state.read_run(proj, slot="service") is None
    assert not (proj / ".agentic" / "pipelines" / f"{PIPE}.yaml").exists()
    # V-01/V-02: созданный служебный юнит (TODO-0003) откатан.
    assert not (proj / ".agentic" / "inbox" / "TODO-0003.md").exists()
    assert _main_bytes(proj) == before


def test_service_finish_refused_while_running(tmp_git_repo: Path, monkeypatch) -> None:
    """Закрытие, пока служебный юнит ещё исполняется — отказ (дождаться
    verify/salvage или awf_kill). Состояние служебного не тронуто."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    _fake_start(monkeypatch, proj)
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"
    monkeypatch.setattr(
        "awf.api._liveness.resolve", lambda project_dir: (True, 4242, "pid_file")
    )
    fin = api.run_service_finish(proj)
    assert fin.action == "refused", fin.message
    assert "running" in fin.message
    svc = run_state.read_run(proj, slot="service")
    assert svc.get("active") is True


def test_service_finish_without_service_is_noop(tmp_git_repo: Path) -> None:
    proj = _project(tmp_git_repo)
    fin = api.run_service_finish(proj)
    assert fin.action == "noop"


# ─── approve служебного юнита: границы ───────────────────────────────────


def test_service_approve_refused_for_foreign_todo(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Approve принимает ТОЛЬКО текущий юнит служебного забега: чужой
    (основной) TODO — отказ, сигнал не публикуется, слоты не пишутся."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    api.run_start(proj, queue=[T1, T2])
    _fake_start(monkeypatch, proj)
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"
    with pytest.raises(AwfApiError, match="service run"):
        api.run_service_approve(proj, T1, evidence="чужой TODO")
    assert not (proj / ".agentic" / "inbox" / f"APPROVE-{T1}.ready").exists()
    assert not (proj / ".agentic" / "context" / f"RUN-EVIDENCE-{T1}.md").exists()
    svc = run_state.read_run(proj, slot="service")
    assert not svc.get("outcomes"), "no verdict for a foreign TODO"


def test_service_approve_requires_evidence(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Служебный юнит — юнит забега: approve без независимого
    верификационного следа — отказ (строгость run-режима)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    _fake_start(monkeypatch, proj)
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"
    with pytest.raises(AwfApiError, match="evidence"):
        api.run_service_approve(proj, res.todo_id, evidence="")


def test_generic_reject_of_service_unit_must_not_touch_main_run(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """M5.2 hard rule (основной забег неприкосновенен): служебный юнит НЕ в
    очереди основного — значит, его reject штатным инструментом верификации
    (api.reject_commit) не должен течь в журнал основного забега, а
    двойной-reject эскалация (stop_run) не должна ОСТАНАВЛИВАТЬ основной:
    это гейт юнитов основного забега, а разовый кандидат в роль — не юнит.

    Красный (дефект, найден QA-проходом юнита): reject_commit пишет в MAIN-слот
    (default) — вердикт/решение служебного юнита меняют main run.yaml, а
    второй reject останавливает основной забег (stop_run, active=False).
    Правильный ответ — отказ (юнит не в очереди основного) или роутинг
    вердикта в служебный слот — решает супервизор; тест пинит только
    инвариант: основной побайто цел и активен."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    _write_todo(proj, T2)
    _launch_first(proj, monkeypatch, [T1, T2])  # index=1, current=T1
    before = _main_bytes(proj)

    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"

    api.reject_commit(
        proj, res.todo_id, reason="кандидат не соответствует описанию"
    )
    assert _main_bytes(proj) == before, (
        "reject of the service unit must not rewrite the main run.yaml "
        "(the unit is not in the main queue — the verdict belongs to the "
        "service slot or to a refusal)"
    )

    # Второй reject (штатный ритуал плохого юнита): эскалация
    # double-reject не должна останавливать MAIN-забег.
    api.reject_commit(proj, res.todo_id, reason="всё ещё не то")
    main = run_state.read_run(proj)
    assert _main_bytes(proj) == before
    assert main.get("active") is True, (
        "double-reject of a service unit must not stop the main run"
    )



def test_service_approve_records_verdict_in_service_slot(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """Вердикт + решение — в служебном слоте: outcomes/decisions, сигнал
    APPROVE-<svc>.ready и RUN-EVIDENCE (гейт движка при активном
    основном забеге)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    _fake_start(monkeypatch, proj)
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"

    out = api.run_service_approve(proj, res.todo_id, evidence="pytest -q → 3 passed")
    assert out.todo_id == res.todo_id
    assert (proj / ".agentic" / "inbox" / f"APPROVE-{res.todo_id}.ready").is_file()
    assert (proj / ".agentic" / "context" / f"RUN-EVIDENCE-{res.todo_id}.md").is_file()
    svc = run_state.read_run(proj, slot="service")
    assert svc["outcomes"][res.todo_id] == {"verdict": "approved"}
    assert svc["decisions"][-1]["kind"] == "approve"
    assert svc["decisions"][-1]["todo_id"] == res.todo_id

def test_concurrent_service_start_rollback_keeps_winner_state(
    tmp_git_repo: Path, monkeypatch
) -> None:
    """A-13-класс (служебный слот): два конкурентных run_service_start —
    один владелец. Откат проигравшего (конфликт в лок-окне) НЕ должен
    стирать состояние ПОБЕДИТЕЛЯ: его забег остаётся активен со своим
    юнитом (index=1, current=юнит); иначе запущенный пайплайн остаётся
    без записи забега, а run_slot_for_todo уводит его downtime в
    основной run (изоляция ломается на пути сбоя).

    Окно детерминировано гейтами: проигравший (поток) проходит
    pre-check «служебного нет» и застаивается в записи пайплайна;
    победитель (поток) между тем записывает active-состояние и входит
    в окно запуска; гейт отпускает проигравшего — его update_run
    встречает конфликт → откат своих побочных эффектов.

    Красный (дефект, найден QA-проходом юнита): откат проигравшего
    (_rollback_service_side_effects) безоговорочно чистит служебный
    слот — вместе со состоянием победителя; позиция-коммит победителя
    пишет пустой файл, служебный забег деградирует в «нет служебного»
    при живом пайплайне."""
    from awf.api import run as api_run_module

    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])

    entered = threading.Event()       # победитель в окне запуска
    loser_in_gate = threading.Event()  # проигравший застаился в гейте
    loser_gate = threading.Event()     # проигравший отпущен в лок-окно
    release = threading.Event()        # победителю — завершить запуск
    loser_tid: dict = {}

    real_pipeline_write = api_run_module._write_service_pipeline

    def gated_pipeline_write(project_dir, slug):
        if threading.get_ident() == loser_tid.get("tid"):
            loser_in_gate.set()
            loser_gate.wait(timeout=30)
        return real_pipeline_write(project_dir, slug)

    def fake_start(project_dir, **kw):
        entered.set()
        release.wait(timeout=30)
        return api_pipeline.StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_run_module, "_write_service_pipeline", gated_pipeline_write)
    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    results: dict = {}
    loser_raised = {"conflict": False}

    def loser() -> None:
        loser_tid["tid"] = threading.get_ident()
        try:
            api.run_service_start(proj, slug="sla-b", description="d")
        except AwfApiError as e:
            loser_raised["conflict"] = "concurrently" in str(e)

    def winner() -> None:
        results["a"] = api.run_service_start(
            proj, slug="sla-a", description="d"
        )

    t_lose = threading.Thread(target=loser)
    t_lose.start()
    # Проигравший в гейте (pre-check пройден: «нет служебного»).
    assert loser_in_gate.wait(timeout=30), "loser never reached the gate"
    w = threading.Thread(target=winner)
    w.start()
    # Победитель записал active-состояние и в окне запуска.
    assert entered.wait(timeout=30), "winner never entered the launch window"
    loser_gate.set()   # проигравший → dispatch + update_run → конфликт
    release.set()      # победитель → позиция-коммит
    t_lose.join(timeout=60)
    w.join(timeout=60)
    assert not t_lose.is_alive() and not w.is_alive()
    assert loser_raised["conflict"], "the loser must hit the in-lock conflict"
    assert results.get("a") is not None and results["a"].action == "started"

    # Победитель должен пережить откат проигравшего.
    svc = run_state.read_run(proj, slot="service")
    assert svc is not None and svc.get("active"), (
        "the loser's rollback must not delete the winner's service state "
        "(an orphaned pipeline with no run record)"
    )
    assert svc["current"] == results["a"].todo_id and svc["index"] == 1
    # Свой откат проигравшего при этом честен: его пайплайн-файл удалён.
    assert not (proj / ".agentic" / "pipelines" / "role-draft-sla-b.yaml").exists()


def test_service_status_reports_both_runs(tmp_git_repo: Path, monkeypatch) -> None:
    """Статус: снимок служебного (юнит, slug, кандидат, pipeline) +
    снимок основного (position, current). Без служебного — ino-op-снимок."""
    proj = _project(tmp_git_repo)
    empty = api.run_service_status(proj)
    assert empty.service_active is False

    _write_todo(proj, T1)
    _write_todo(proj, T2)
    _launch_first(proj, monkeypatch, [T1, T2])
    res = api.run_service_start(proj, slug=SLUG, description="Описание.")
    assert res.action == "started"

    st = api.run_service_status(proj)
    assert st.service_active is True
    assert st.todo_id == res.todo_id and st.slug == SLUG
    assert st.candidate_path == CAND and st.pipeline == PIPE
    assert st.main_active is True and st.main_current == T1


def test_service_slot_corrupt_file_degrades(tmp_git_repo: Path, monkeypatch) -> None:
    """Порванный service-run.yaml деградирует в «нет служебного» (тот же
    класс деградации, что и AUD02-06 у основного run.yaml) — без
    traceback; основной забег от этого не страдает."""
    import yaml

    proj = _project(tmp_git_repo)
    _write_todo(proj, T1)
    api.run_start(proj, queue=[T1])
    svc_file = run_state.run_file(proj, slot="service")
    svc_file.parent.mkdir(parents=True, exist_ok=True)
    svc_file.write_text("queue: [TODO-00\nbroken: [", encoding="utf-8")
    assert run_state.read_run(proj, slot="service") is None
    st = api.run_service_status(proj)
    assert st.service_active is False
    fin = api.run_service_finish(proj)
    assert fin.action == "noop"
    assert run_state.read_run(proj).get("active") is True
    # И не-связная проверка: yaml.safe_load на мусоре не должен ронять API.
    with pytest.raises(Exception):
        yaml.safe_load("queue: [TODO-00\nbroken: [")


# ─── e2e: сквозные shim-сценарии (реальный движок в подпроцессе) ─────────


def _make_e2e_project(tmp_path: Path) -> Path:
    """Git-проект: .agentic-скелет + 4-стадийный default.yaml + T1/T2.
    Служебный юнит получит TODO-0003 (max existing + 1)."""
    proj = tmp_path / "svc"
    proj.mkdir()
    _git_init(proj)
    ag = proj / ".agentic"
    for sub in ("pipelines", "roles", "inbox", "outbox", "context", "logs", "state"):
        (ag / sub).mkdir(parents=True)
    (proj / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "ignore .agentic"], cwd=proj, check=True)
    (ag / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (ag / "pipelines" / "default.yaml").write_text(DEFAULT_4STAGE, encoding="utf-8")
    (ag / "roles" / "worker.md").write_text("# Worker\nExecute the TODO.\n", encoding="utf-8")
    (ag / "roles" / "supervisor.md").write_text("# Supervisor\nPlan and verify.\n", encoding="utf-8")
    inbox = ag / "inbox"
    for todo_id in (T1, T2):
        (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nstub task\n", encoding="utf-8")
        (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    return proj


def _shim_env(monkeypatch, tmp_path: Path, state_name: str, extra: dict | None = None) -> None:
    """Шим-среда для фонового ребёнка (`python -m awf start ...`): он
    наследует os.environ тест-процесса (start_in_background копирует его).
    plan-стадия переписывает TODO-0003 (служебный юнит) — поэтому
    AWF_SHIM_PLAN_TODO_ID = его id, а не дефолтный TODO-0001 (файл
    основного T1)."""
    state_dir = tmp_path / state_name
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv("PATH", f"{SHIM_DIR}:{os.environ['PATH']}")
    monkeypatch.setenv("AWF_SHIM_MODE", "do-step")
    monkeypatch.setenv("AWF_SHIM_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AWF_SHIM_PLAN_TODO_ID", "TODO-0003")
    monkeypatch.delenv("AWF_TEST_OPENCODE_BEHAVIOR", raising=False)
    monkeypatch.delenv("AWF_SHIM_SUPERVISOR_MODE", raising=False)
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)


def _wait_until(pred, timeout: float = 180, what: str = "condition") -> bool:
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


def _main_snapshot(proj: Path) -> dict:
    st = run_state.read_run(proj)
    return {
        "bytes": _main_bytes(proj),
        "active": bool(st.get("active")),
        "queue": st.get("queue"),
        "index": st.get("index"),
        "current": st.get("current"),
        "completed": st.get("completed"),
        "decisions": st.get("decisions"),
    }


@pytest.mark.timeout(300)
def test_service_launch_end_to_end_creates_candidate(tmp_path, monkeypatch) -> None:
    """(а)+(в)+(1) e2e: служебный запуск идёт штатным движком (4 стадии,
    shim-воркеры); declared output стадии-исполнителя (кандидат в draft)
    создаётся и движком засчитывается (свежесть M3.2); основной run.yaml
    побайто неприкосновенен на всём проходе; после закрытия — «продолжай
    основной». Красный до фикса: api.run_service_start отсутствует."""
    proj = _make_e2e_project(tmp_path)
    api.run_start(proj, queue=[T1, T2])
    before = _main_snapshot(proj)

    _shim_env(monkeypatch, tmp_path, "state-ok",
              {"AWF_SHIM_CREATE_DECLARED_OUTPUT": "1"})

    res = api.run_service_start(
        proj, slug=SLUG, description="Аудит безопасности кода."
    )
    assert res.action == "started", res.message
    svc = res.todo_id
    assert svc == "TODO-0003", f"the service unit must be the next free id: {svc}"
    try:
        # Прогон дождётся verify-стадии: кандидат создан стадией-исполнителем,
        # пайплайн ждёт сигнал (ACK сим-супервизора гейтится evidence'ом —
        # его даёт run_service_approve, как супервизор в реальности).
        assert _wait_until(
            lambda: _stage_name(proj) == "verify", what="verify stage"
        ), f"the pipeline never reached verify. stage={_stage_name(proj)!r}"
        api.run_service_approve(
            proj, svc, evidence="e2e: кандидат проверен (shim); verdict: approve"
        )
        assert _wait_until(
            lambda: not _pipeline_alive(proj), what="pipeline exit"
        ), "the pipeline must complete after the service approve"
    finally:
        if _pipeline_alive(proj):
            from awf.api import kill_pipeline

            kill_pipeline(proj)

    # (в) Кандидат создан declared output'ом (содержимое shim-исполнителя).
    cand = proj / CAND
    assert cand.is_file(), "the draft candidate (declared output) is missing"
    assert "shim output" in cand.read_text(encoding="utf-8")

    # (1) Основной забег побайто как до служебного.
    after = _main_snapshot(proj)
    assert after["bytes"] == before["bytes"], "the main run.yaml must be byte-identical"
    assert after["queue"] == before["queue"]
    assert after["index"] == before["index"]
    assert after["current"] == before["current"]
    assert after["decisions"] == before["decisions"]

    # Служебное состояние: юнит запущен, забег активен.
    svc_state = run_state.read_run(proj, slot="service")
    assert svc_state.get("active") is True
    assert svc_state["current"] == svc and svc_state["index"] == 1

    # (г) Закрытие: ответ называет продолжение основного.
    fin = api.run_service_finish(proj)
    assert fin.action == "stopped", fin.message
    assert "awf_run_next" in fin.next_action and "awf_continue" in fin.next_action
    final = _main_snapshot(proj)
    assert final["bytes"] == before["bytes"]
    assert final["active"] is True


@pytest.mark.timeout(300)
def test_service_declared_output_stale_stops_the_stage(tmp_path, monkeypatch) -> None:
    """(в) e2e-сценарий M3.2: кандидат ОСТАЛСЯ от прежней попытки (старый
    mtime), исполнитель не обновляет его (shim без создания declared
    output). Движок НЕ засчитывает старую работу: стадия уходит в
    failure-путь (on_blocked: stop), пайплайн останавливается, кандидат не
    изменён, основной забег побайто цел."""
    proj = _make_e2e_project(tmp_path)
    cand = proj / CAND
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text("leftover candidate from a previous attempt\n", encoding="utf-8")
    past = time.time() - 3600
    os.utime(cand, (past, past))
    old_mtime = cand.stat().st_mtime

    api.run_start(proj, queue=[T1, T2])
    before = _main_snapshot(proj)
    _shim_env(monkeypatch, tmp_path, "state-stale")  # без CREATE_DECLARED_OUTPUT

    res = api.run_service_start(proj, slug=SLUG, description="Аудит.")
    assert res.action == "started", res.message
    try:
        assert _wait_until(
            lambda: not _pipeline_alive(proj), timeout=240, what="pipeline stop"
        ), "the pipeline must stop on the unfulfilled declared output"
    finally:
        if _pipeline_alive(proj):
            from awf.api import kill_pipeline

            kill_pipeline(proj)

    # (в) Проверка свежести сработала: причина — stale declared output.
    assert res.log_file and Path(res.log_file).is_file()
    log_text = Path(res.log_file).read_text(encoding="utf-8", errors="replace")
    assert "declared output" in log_text and "stale" in log_text, (
        "the engine must name the stale declared output as the stop reason"
    )
    # Кандидат не тронут: тот же контент и старый mtime (стадия не прошла).
    assert cand.read_text(encoding="utf-8") == "leftover candidate from a previous attempt\n"
    assert cand.stat().st_mtime == old_mtime

    assert _main_snapshot(proj)["bytes"] == before["bytes"]
    fin = api.run_service_finish(proj, reason="stale candidate — worker did not refresh")
    assert fin.action == "stopped", fin.message
    assert _main_snapshot(proj)["bytes"] == before["bytes"]


@pytest.mark.timeout(300)
def test_service_run_failure_keeps_main_intact(tmp_path, monkeypatch) -> None:
    """(д) e2e-деградация: исполнитель служебного умирает (crash, без
    сигнала) — ретраи исчерпаны, пайплайн остановлен. Основной забег
    активен и побайто цел; служебный закрывается с репортом; основной
    продолжает существовать со своим current/index."""
    proj = _make_e2e_project(tmp_path)
    api.run_start(proj, queue=[T1, T2])
    before = _main_snapshot(proj)
    # AWF_SUPERVISOR_TIMEOUT=15: the salvage stage (worker died, no signal)
    # is an interactive wait in this launch — a short timeout makes it time
    # out and stop the pipeline instead of hanging for the 3600s default.
    _shim_env(
        monkeypatch, tmp_path, "state-crash",
        {"AWF_SHIM_STAGE_MODES": "agent-implementer=crash",
         "AWF_SUPERVISOR_TIMEOUT": "15"},
    )

    res = api.run_service_start(proj, slug=SLUG, description="Аудит.")
    assert res.action == "started", res.message
    svc = res.todo_id
    try:
        assert _wait_until(
            lambda: not _pipeline_alive(proj), timeout=240, what="pipeline death"
        ), "the crashed service pipeline must die (retries exhausted)"
    finally:
        if _pipeline_alive(proj):
            from awf.api import kill_pipeline

            kill_pipeline(proj)

    # Доказательство, что движок ДОШЁЛ до стадии-исполнителя (не умер на
    # plan): в оркестратор-логе остановка именно на agent-implementer.
    orch = (proj / ".agentic" / "logs" / "orchestrator.log")
    if orch.is_file():
        orch_text = orch.read_text(encoding="utf-8", errors="replace")
        assert "agent-implementer" in orch_text and (
            "exited with code 3" in orch_text or "stopped" in orch_text.lower()
        ), "the pipeline must have reached (and died at) the worker stage"

    # (д) Основной — как был: побайто, активен, позиция не тронута.
    after = _main_snapshot(proj)
    assert after["bytes"] == before["bytes"], "the main run must survive the service death"
    assert after["active"] is True
    assert after["queue"] == before["queue"]
    assert after["index"] == before["index"]
    assert after["current"] == before["current"]

    # Служебный забег: юнит был запущен (position зафиксирован), забег
    # активен — его закрывает супервизор с причиной.
    svc_state = run_state.read_run(proj, slot="service")
    assert svc_state.get("active") is True
    assert svc_state["current"] == svc and svc_state["index"] == 1

    fin = api.run_service_finish(proj, reason="service worker died (crash)")
    assert fin.action == "stopped", fin.message
    assert fin.report_file and Path(fin.report_file).is_file()
    assert _main_snapshot(proj)["bytes"] == before["bytes"]
