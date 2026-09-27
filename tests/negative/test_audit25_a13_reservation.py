"""A-13 (расширение, повторная проверка аудита 2026-09-27 V-01/V-02):
резервация позиции очереди откатывается на ЛЮБОМ отказном выходе
``run_next``.

``test_audit25_a13_run_cas.py`` фиксирует CAS-семантику резервации
(конкурентность, перехват мёртвого резерва, поколение). Здесь —
откат резервации для двух отказных переходов, добавленных TODO-0123
(foreground с ненулевым исходом, исключение из ``start_pipeline``):
``current`` возвращается к предзапусковому значению, позиция снова
свободна, повторная резервация того же элемента проходит.

Findings covered:
- test_foreground_failure_releases_reservation — foreground-fail
- test_launch_exception_releases_reservation — крах запуска
"""
from __future__ import annotations

from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state
from awf.api._results import StartResult


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A13ReservationRollback")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _assert_reservation_released(proj: Path, before: dict) -> None:
    """Резервация снята: позиция очереди вернулась к предзапусковому
    состоянию (current, index), забег жив, очередь цела."""
    after = run_state.read_run(proj)
    assert after["current"] == before["current"], (
        f"резервация обязана снять current обратно в {before['current']!r}, "
        f"получено {after['current']!r} — отказанный запуск не может владеть "
        "позицией очереди"
    )
    assert after["index"] == before["index"], (
        f"индекс обязан остаться {before['index']}, получено {after['index']}"
    )
    assert after["active"] is True, "отказ запуска не закрывает забег"
    assert after["queue"] == before["queue"], "отказ запуска не трогает очередь"
    assert after["generation"] == before["generation"], (
        "отказ запуска не меняет поколение забега"
    )


def test_foreground_failure_releases_reservation(tmp_git_repo, monkeypatch):
    """V-01: foreground-запуск с ненулевым исходом — отказ; резервация
    ``current`` снимается, и повторный запуск той же позиции проходит
    (позиция не заперта отказом)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    counter: dict = {"n": 0}

    def fake_start(project_dir, **kw):
        counter["n"] = counter.get("n", 0) + 1
        if counter["n"] == 1:
            return StartResult(
                run_mode="foreground", run_id=None, log_file=None,
                exit_code=1, message="Pipeline completed with exit code 1",
            )
        return StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    before = run_state.read_run(proj)
    assert before["current"] == "" and before["index"] == 0

    refused = api.run_next(proj, background=False)
    assert refused.action == "refused", f"ожидался отказ: {refused.message}"
    _assert_reservation_released(proj, before)

    retry = api.run_next(proj)
    assert retry.action == "started", (
        f"повтор после foreground-отказа обязан пройти: {retry.message}"
    )
    assert counter["n"] == 2, "повтор — ровно второй запуск"
    after = run_state.read_run(proj)
    assert after["current"] == "TODO-0001", "успешный запуск снова резервирует позицию"
    assert after["index"] == 1


def test_launch_exception_releases_reservation(tmp_git_repo, monkeypatch):
    """V-02: крах ``start_pipeline`` — отказ; резервация ``current``
    снимается (baseline оставлял current='TODO-0001' без живого
    пайплайна — запертая позиция), повторный запуск той же позиции
    проходит."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    counter: dict = {"n": 0}

    def fake_start(project_dir, **kw):
        counter["n"] = counter.get("n", 0) + 1
        if counter["n"] == 1:
            raise RuntimeError("boom (fake launch crash)")
        return StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    before = run_state.read_run(proj)
    assert before["current"] == "" and before["index"] == 0

    refused = api.run_next(proj)
    assert refused.action == "refused", (
        f"крах обязан стать классифицированным отказом: {refused.message}"
    )
    _assert_reservation_released(proj, before)

    retry = api.run_next(proj)
    assert retry.action == "started", (
        f"повтор после краха обязан пройти: {retry.message}"
    )
    assert counter["n"] == 2, "повтор — ровно второй запуск"
    after = run_state.read_run(proj)
    assert after["current"] == "TODO-0001"
    assert after["index"] == 1
