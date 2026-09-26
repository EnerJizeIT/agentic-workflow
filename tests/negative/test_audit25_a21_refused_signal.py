"""A-21 (аудит 2026-09-25, слой 12): отказ run_next не оставляет сигнал TODO.

Дефект (воспроизведён в аудите): ``run_next`` создаёт ``inbox/{id}.ready``
ДО ``start_pipeline``; при ``run_mode="noop"/"error"`` возвращает отказ
(``action="refused"``), индекс оставляет на месте — но ``.ready`` не
удаляет. После отказа ``newest_active``/``list_active_todos`` уже считают
TODO активным: состояние расходится с ответом ``refused``, а случайный
``awf_start`` может подхватить не запущенный TODO.

Инварианты:
1. Отказ запуска восстанавливает прежнее состояние сигнала: если
   ``.ready`` не было — его нет; если было — оно на месте (и с тем же
   содержимым).
2. Индекс очереди и поколение при отказе не меняются; резервация
   ``current`` откатывается (A-13).
3. Повторный ``run_next`` после отказа запускает тот же элемент ровно
   один раз.

Findings covered:
- test_refused_run_next_leaves_no_ready_signal — TODO без ``.ready``,
  ``start_pipeline`` возвращает ``run_mode="error"``: baseline сигнал
  остаётся (красный), фикс — его нет, индекс/поколение не изменены
- test_refused_run_next_with_noop_leaves_no_ready_signal — то же для
  ``run_mode="noop"`` (вторая половина того же ветвления)
- test_refused_run_next_keeps_preexisting_ready — ранее существовавший
  ``.ready`` (dispatch, ручной touch) отказом не уничтожается
- test_refused_run_next_retry_launches_same_item_once — повторный
  ``run_next`` после отказа: один неудачный вызов + ровно один
  успешный запуск того же элемента, индекс сдвинулся один раз
"""
from __future__ import annotations

from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A21RefusedSignal")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _ready_path(proj: Path, todo_id: str = "TODO-0001") -> Path:
    return proj / ".agentic" / "inbox" / f"{todo_id}.ready"


def _fake_start_refused(monkeypatch, run_mode: str = "error", counter: dict | None = None):
    """``start_pipeline`` — заглушка, возвращающая отказ запуска.

    ``counter["n"]`` (если передан) считает ВСЕ вызовы; ``counter["modes"]``
    — run_mode по номеру вызова, когда поведение меняется между вызовами
    (повторный запуск: первый вызов отказ, второй — успех).
    """
    from awf.api._results import StartResult

    def fake_start(project_dir, **kw):
        mode = run_mode
        if counter is not None:
            counter["n"] = counter.get("n", 0) + 1
            modes = counter.get("modes") or []
            if counter["n"] <= len(modes):
                mode = modes[counter["n"] - 1]
        return StartResult(
            run_mode=mode,
            run_id=424242 if mode not in ("noop", "error") else None,
            log_file=None,
            exit_code=0,
            message="ok" if mode not in ("noop", "error") else "launch failed (fake)",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _assert_run_state_unchanged(proj: Path, before: dict) -> None:
    """Инвариант 2: отказ не двигает очередь — индекс, текущий элемент
    (резервация откатилась) и поколение как до run_next."""
    after = run_state.read_run(proj)
    assert after["index"] == before["index"], (
        f"индекс обязан остаться {before['index']}, получено {after['index']}"
    )
    assert after["current"] == before["current"], (
        f"текущий элемент обязан остаться {before['current']!r} "
        f"(резервация откатилась), получено {after['current']!r}"
    )
    assert after["generation"] == before["generation"], (
        "поколение забега обязан остаться без изменений при отказе"
    )
    assert after["active"] is True, "отказ запуска не закрывает забег"
    assert after["queue"] == before["queue"], "отказ запуска не трогает очередь"


def test_refused_run_next_leaves_no_ready_signal(tmp_git_repo, monkeypatch):
    """A-21.1: TODO без ``.ready``, запуск отказан (run_mode="error") —
    сигнал, созданный run_next, обязан исчезнуть; индекс не изменён.
    Baseline: сигнал остаётся (красный)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists(), "исходно сигнала быть не должно"

    _fake_start_refused(monkeypatch, run_mode="error")
    before = run_state.read_run(proj)

    result = api.run_next(proj)

    assert result.action == "refused", f"ожидался отказ, получено {result.action}"
    assert "TODO-0001" in result.todo_id
    assert not ready.exists(), (
        "отказ запуска обязан удалить .ready, который run_next сам создал — "
        "иначе newest_active видит активный TODO, который никуда не запущен"
    )
    _assert_run_state_unchanged(proj, before)


def test_refused_run_next_with_noop_leaves_no_ready_signal(tmp_git_repo, monkeypatch):
    """A-21.1 (noop-ветка): то же для run_mode="noop" — «Pipeline already
    running» / отказ аренды: чужой отказ запуска тоже не оставляет сигнал."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists()

    _fake_start_refused(monkeypatch, run_mode="noop")
    before = run_state.read_run(proj)

    result = api.run_next(proj)

    assert result.action == "refused"
    assert not ready.exists(), "noop-отказ запуска не обязан оставлять .ready"
    _assert_run_state_unchanged(proj, before)


def test_refused_run_next_keeps_preexisting_ready(tmp_git_repo, monkeypatch):
    """А-21, инвариант 1 (вторая половина): ``.ready``, существовавший ДО
    run_next (dispatch / ручной touch), отказом не уничтожается — и
    содержимое остаётся прежним."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    ready = _ready_path(proj)
    ready.write_text("dispatch\n", encoding="utf-8")
    api.run_start(proj, queue=["TODO-0001"])

    _fake_start_refused(monkeypatch, run_mode="error")
    before = run_state.read_run(proj)

    result = api.run_next(proj)

    assert result.action == "refused"
    assert ready.exists(), "ранее существовавший .ready отказом не уничтожается"
    assert ready.read_text(encoding="utf-8") == "dispatch\n", (
        "содержимое предсуществующего .ready не меняется"
    )
    _assert_run_state_unchanged(proj, before)


def test_refused_run_next_retry_launches_same_item_once(tmp_git_repo, monkeypatch):
    """A-21, инвариант 3: повторный run_next после отказа запускает тот же
    элемент ровно один раз (один неудачный вызов + один успешный), индекс
    сдвигается один раз, сигнал появляется только с успешным запуском."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists()

    counter: dict = {"modes": ["error", "background"]}
    _fake_start_refused(monkeypatch, counter=counter)

    first = api.run_next(proj)
    assert first.action == "refused", "первый запуск обязан быть отказан"
    assert not ready.exists(), "после отказа сигнала нет"

    second = api.run_next(proj)

    assert second.action == "started", f"повторный запуск обязан пройти: {second.message}"
    assert second.todo_id == "TODO-0001", "повтор запускает тот же элемент"
    assert counter.get("n") == 2, (
        f"ровно два вызова start_pipeline (отказ + запуск), было {counter.get('n')}"
    )
    assert ready.exists(), "сигнал появляется с успешным запуском"
    state = run_state.read_run(proj)
    assert state["index"] == 1, "индекс сдвинулся ровно один раз"
    assert state["current"] == "TODO-0001"
    assert state["generation"] == 1, "повторный запуск не меняет поколение"
