"""REPORTS30 (бэклог «Мелочи»): defense-in-depth резервации run_next —
mid-window (между claim и фактическим запуском стадии).

Карта покрытия (что уже зашито соседними тестами и чего не хватало):
- оба pre-read ДО claim → CAS резервации отклоняет проигравшего
  (test_audit25_a13_run_cas.py::test_two_concurrent_run_next_launch_one_item_once);
- поздний pre-read при ЖИВОМ пайплайне (pid зарегистрирован) → отказ по
  живости (TODO-0103, ...::test_late_pre_read_after_reservation_refuses_when_pipeline_live);
- claim на диске + пайплайна нет (крах между резервацией и запуском) →
  перехват мёртвого резерва, элемент завершается ровно один раз
  (...::test_dead_reservation_taken_over_and_launched_once);
- не покрыто: claim на диске, стадия ещё не стартовала (пайплайн НЕ жив —
  ни PID-файла, ни pipeline_pid), но запуск ИДЁТ (launch lease удерживается
  живым процессом). На диске это выглядит точно как мёртвая резервация —
  дискриминатор не в run.yaml, а в lease.

Механизм mid-window (по коду, фиксация для DONE):
1. Резервация ``update_run_cas(generation, index, current)`` пишет
   ``current=next_id`` ДО окна запуска (awf/api/run.py:1019). На диске в
   окне: ``index`` — прежний, ``current=next_id``.
2. Конкурентный вызов pre-read'ит ``cur == next_id`` — ветка TODO-0103:
   пайплайн жив → отказ; мёртв → fall-through на повторную резервацию
   (проектное решение: крах не должен запереть очередь, TODO-0082).
3. Повторная резервация CAS совпадает со-state на диске (кортеж посчитан
   по снапшоту уже зарезервированной позиции) — на уровне run-state
   владение в этом окне НЕ удерживается, повторный «резерв» проходит.
4. Реальная защита от двойного запуска — launch lease внутри
   ``start_pipeline`` (awf/api/pipeline.py:1119, A-02): второй вызов
   получает ``run_mode="noop"`` («Launch refused: another launch is
   already in progress») → ``launch_failed`` → ``run_next`` откатывает
   только собственные эффекты (A-21: предсуществующий ``.ready`` не
   трогается), снимает собственную резервацию (no-op: его ``cur`` уже
   ``next_id``) и отказывается. Claim владельца цел: index не сдвинут,
   current не разрушен, generation не изменён, lease не удалён.

Инварианты (зафиксированы как текущий дизайн):
1. Повторный вход в mid-window НЕ даёт второго запуска: элемент
   запущен ровно один раз (владелец), входящий вызов отказан текстом
   отказа запуска («Launch failed: Launch refused: another launch is
   already in progress»).
2. Claim владельца выживает: index/current/generation/queue не
   изменены, предсуществующий ``.ready`` (сигнал владельца) не удалён,
   lease владельца не тронут.
3. Отказ идёт через спроектированный fall-through: входящий вызов
   повторнорезервировал (два CAS с ``current=next_id``) и отказан в
   окне запуска, а не до него. Плоский отказ при ``cur == next_id``
   (без живости/lease) — отклонённый вариант TODO-0103, он запер бы
   очередь после краха.
4. Собственные побочные эффекты входящего вызова откатываются: ``.ready`,
   которого не было до его входа, создаётся и затем удаляется отказом.

Findings covered:
- test_mid_window_reentry_refused_no_double_launch — главный пин:
  claim на диске + живая lease + живого пайплайна нет → отказ, второй
  запуск не случился, claim и сигнал владельца целы, CAS-след
  (резерв + релиз) доказывает проход через спроектированную ветку
- test_mid_window_reentry_rolls_back_own_ready — вторая половина
  A-21 в mid-window: сигнала не было → отказ не оставляет его

Зубы (мутации, scratch-копия — детали и вывод прогонов в DONE-TODO-0188.md):
- M1: ``launch_failed`` перестаёт считать ``noop`` → входящий вызов
  «успешен» и коммитит index+1 поверх идущего запуска владельца →
  красный (action/index)
- M2: плоский отказ при ``cur == next_id`` (ветка TODO-0103 без
  живости) → отказ до окна запуска, CAS-след пуст, другой текст →
  красный (message/trace)
- M3: безусловное удаление ``.ready`` в отказной ветке → сигнал
  владельца уничтожается → красный (ready.exists)
"""
from __future__ import annotations

import os
from pathlib import Path

import awf.api.pipeline as api_pipeline
import awf.run_state as run_state_mod
from awf import api, run_state
from awf.api import _lease as lease_mod
from awf.api._results import StartResult


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="R30MidWindow")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _ready_path(proj: Path) -> Path:
    return proj / ".agentic" / "inbox" / "TODO-0001.ready"


def _mid_window_project(tmp_git_repo: Path) -> Path:
    """Проект с mid-window на диске: claim владельца записан
    (``current=TODO-0001``, ``index=0``), стадия ещё не стартовала
    (PID-файла нет, ``pipeline_pid`` нет), а запуск ИДЁТ — launch lease
    удерживает живой процесс (тестовый; роль «запуска владельца»)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    # claim владельца — то, что резервация run_next пишет до окна запуска
    run_state.write_run(proj, current="TODO-0001")
    # lease владельца: живой процесс (тест) — как production, где
    # launch в окне запуска держит lease со своим pid
    lease_mod.lease_path(proj).write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert not (proj / ".agentic" / "logs" / "awf-start.pid").is_file(), (
        "mid-window: PID-файла быть не должно — стадия ещё не стартовала"
    )
    state = run_state.read_run(proj)
    assert state["index"] == 0 and state["current"] == "TODO-0001"
    return proj


def _install_lease_guarded_fake_start(counters: dict, monkeypatch, proj: Path) -> None:
    """Подмена ``start_pipeline``: production-гард — настоящий
    ``_lease.acquire`` (живой владелец → ``LeaseHeldError`` →
    ``run_mode="noop"`` с production-текстом, как
    awf/api/pipeline.py:1121-1127); сам спавн не эмулируется. Если
    гард сломан (мутация), «второй запуск» пройдёт и попадёт в счётчик
    ``launches`` — тест краснеет."""

    def fake_start(project_dir, **kw):
        try:
            lease = lease_mod.acquire(project_dir)
        except lease_mod.LeaseHeldError as e:
            counters["refused"] += 1
            return StartResult(
                run_mode="noop", run_id=None, log_file=None,
                exit_code=0, message=f"Launch refused: {e}",
            )
        # гард не сработал — именно здесь пошёл бы двойной запуск
        lease.release()
        counters["launches"] += 1
        return StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)


def _cas_trace(monkeypatch) -> list:
    """След вызовов ``update_run_cas`` (условные записи run_next):
    mid-window-повтор обязан пройти ровно через два — резерв
    (повторный, ``current=next_id``) и релиз; соседние отказы дают
    другой след (live-отказ живости — ноль, CAS-miss — один)."""
    real = run_state_mod.update_run_cas
    trace: list = []

    def wrapped(project_dir, mutator, **kw):
        trace.append({k: kw.get(k) for k in ("generation", "index", "current")})
        return real(project_dir, mutator, **kw)

    monkeypatch.setattr(run_state_mod, "update_run_cas", wrapped)
    return trace


def _assert_owner_claim_intact(proj: Path) -> None:
    """Инвариант 2: повторный вход не трогает позицию владельца."""
    state = run_state.read_run(proj)
    assert state["index"] == 0, (
        f"индекс обязан остаться 0 (claim владельца), получено {state['index']}"
    )
    assert state["current"] == "TODO-0001", (
        f"claim владельца обязан остаться целым, получено {state['current']!r}"
    )
    assert state["generation"] == 1, "поколение забега не меняется при отказе"
    assert state["active"] is True, "отказ повторного входа не закрывает забег"
    assert state["queue"] == [{"todo_id": "TODO-0001", "pipeline": ""}], (
        "очередь повторным входом не трогается"
    )


def test_mid_window_reentry_refused_no_double_launch(tmp_git_repo, monkeypatch):
    """Главный пин: claim на диске, стадия не стартовала, запуск идёт
    (lease у живого процесса). Повторный ``run_next`` НЕ захватывает
    элемент повторно: второй запуск не случился, входящий вызов
    отказан текстом отказа запуска, claim и сигнал владельца целы."""
    proj = _mid_window_project(tmp_git_repo)
    ready = _ready_path(proj)
    # сигнал владельца: он трогает .ready ДО окна запуска (run_next),
    # поэтому на момент повторного входа он уже существует
    ready.touch()
    counters: dict = {"launches": 0, "refused": 0}
    _install_lease_guarded_fake_start(counters, monkeypatch, proj)
    trace = _cas_trace(monkeypatch)

    result = api.run_next(proj)

    assert result.action == "refused", (
        f"повторный вход в mid-window обязан быть отказом, "
        f"получено {result.action!r}: {result.message}"
    )
    assert "another launch is already in progress" in result.message, (
        f"отказ обязан текстом называть идущий запуск (lease), "
        f"получено: {result.message}"
    )
    assert counters["launches"] == 0, (
        f"второй запуск случился ({counters['launches']}) — mid-window "
        "не закрыт: повторный захват элемента"
    )
    assert counters["refused"] == 1, (
        f"входящий вызов обязан один раз встретить идущий запуск "
        f"(lease-отказ), встречено {counters['refused']}"
    )
    # инвариант 3: отказ — через спроектированный fall-through
    # (повторная резервация + релиз, оба CAS по зарезервированной
    # позиции), а не через соседний отказ до окна запуска
    assert trace == [
        {"generation": 1, "index": 0, "current": "TODO-0001"},
        {"generation": 1, "index": 0, "current": "TODO-0001"},
    ], (
        f"CAS-след обязан быть «резерв + релиз» по зарезервированной "
        f"позиции, получен: {trace}"
    )
    _assert_owner_claim_intact(proj)
    assert ready.exists(), (
        "предсуществующий .ready (сигнал владельца) отказом не уничтожается — A-21"
    )
    lease = lease_mod.lease_path(proj)
    assert lease.is_file() and lease.read_text(encoding="utf-8").strip() == str(os.getpid()), (
        "lease владельца повторным входом не трогается"
    )


def test_mid_window_reentry_rolls_back_own_ready(tmp_git_repo, monkeypatch):
    """Вторая половина A-21 в mid-window: сигнала до входа нет —
    ``run_next`` создаёт его перед окном запуска и обязан удалить на
    отказе (иначе newest_active видит активный TODO, который никуда не
    запущен). Claim владельца при этом цел."""
    proj = _mid_window_project(tmp_git_repo)
    ready = _ready_path(proj)
    assert not ready.exists(), "исходно сигнала быть не должно"
    counters: dict = {"launches": 0, "refused": 0}
    _install_lease_guarded_fake_start(counters, monkeypatch, proj)

    result = api.run_next(proj)

    assert result.action == "refused", (
        f"повторный вход обязан быть отказом, получено {result.action!r}: "
        f"{result.message}"
    )
    assert counters["launches"] == 0, "второй запуск не должен случиться"
    assert not ready.exists(), (
        "отказанный повторный вход обязан удалить .ready, который сам создал"
    )
    _assert_owner_claim_intact(proj)
