"""A-13 (аудит 2026-09-25, слой 2): условные переходы состояния забега.

Дефект (воспроизведён в аудите на временных проектах): ``run_start`` читает
активность забега ДО блокировки, затем пишет без условной проверки — два
параллельных ``run_start(force=False)`` оба успешны, очередь последнего
затирает первую. ``run_next`` снимает индекс до запуска пайплайна, а
финальный mutator проверяет только ``active`` — при изменении состояния
в окне запуска владение текущим элементом не проверяется: конкурентный
``run_next`` запускает тот же элемент второй раз.

Инварианты:
1. ``run_start(force=False)``: переход ``inactive → active`` — один lock
   (read-modify-write); два конкурентных старта → ровно один ``started``,
   второй — внятный отказ, очередь победителя цела.
2. ``run_next``: переход очереди условный по (поколение забега, индекс,
   текущий элемент); конкурентные вызовы запускают элемент ровно один раз,
   проигравший получает отказ, индекс двигается один раз.
3. Поколение забега — аддитивное поле run.yaml (отсутствие = 0); старые
   файлы читаются, ``force=True`` продолжает перезаписывать.

Findings covered:
- test_two_concurrent_run_starts_one_wins — два потока ``run_start`` через
  барьер в обёртке ``run_state.update_run``: baseline оба успешны (красный),
  фикс — один start, второй отказ, очередь победителя цела
- test_two_concurrent_run_next_launch_one_item_once — ``start_pipeline``
  заглушка со счётчиком; baseline два запуска (красный), фикс — один
  запуск, индекс +1, проигравший отказ
- регрессы: одиночный start/next/status/finish, ``force=True`` замена,
  чтение состояния без поля поколения
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import awf.api.pipeline as api_pipeline
import awf.run_state as run_state_mod
from awf import api, run_state


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A13RunCas")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _barrier_update_run(real_update_run, arrivals: dict, timeout: float = 15.0):
    """Двухсторонний барьер вокруг ``run_state.update_run``.

    Оба конкурентных вызова обязаны дойти до условной записи: первый
    ждёт второго, второй отпускает обоих. Таймаут — защита сьюта: если
    второй вызов так и не пришёл (базовый код не ходит через
    ``update_run``), одиночный вызов не зависает, а идёт дальше.
    """
    lock = threading.Lock()
    both = threading.Event()

    def wrapped(project_dir, mutator):
        with lock:
            arrivals["n"] += 1
            wait = arrivals["n"] == 1
        if wait:
            both.wait(timeout=timeout)
        else:
            both.set()
        return real_update_run(project_dir, mutator)

    return wrapped


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_two_concurrent_run_starts_one_wins(tmp_git_repo, monkeypatch):
    """A-13.1: два конкурентных run_start(force=False) → ровно один старт.

    Барьер в обёртке ``run_state.update_run``: оба потока доходят до
    условной записи. Baseline (проверка активности до lock) — оба
    успешны, очередь победителя затирается (красный). Фикс — один
    ``started``, второй — внятный отказ.
    """
    proj = _project(tmp_git_repo)
    arrivals: dict = {"n": 0}
    monkeypatch.setattr(
        run_state_mod, "update_run", _barrier_update_run(run_state_mod.update_run, arrivals)
    )

    results: dict = {}
    start_barrier = threading.Barrier(2)

    def worker(n: int) -> None:
        start_barrier.wait(timeout=15)
        try:
            results[n] = ("ok", api.run_start(proj, queue=[f"TODO-000{n}"]))
        except api.AwfApiError as exc:
            results[n] = ("refused", str(exc))
        except Exception as exc:  # noqa: BLE001 — surfaced in the asserts
            results[n] = ("error", repr(exc))

    t1 = threading.Thread(target=worker, args=(1,))
    t2 = threading.Thread(target=worker, args=(2,))
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), "старты не завершились"

    assert "error" not in {r[0] for r in results.values()}, results
    statuses = {results[1][0], results[2][0]}
    assert statuses == {"ok", "refused"}, (
        f"ожидался один старт и один отказ, получили {statuses}: "
        f"{ {k: v[1] if isinstance(v[1], str) else v[1].message for k, v in results.items()} }"
    )
    assert arrivals["n"] == 2, (
        f"оба старта обязаны дойти до условной записи, дошли {arrivals['n']} — "
        "тест проверил не то (barrier не сработал)"
    )
    winner = 1 if results[1][0] == "ok" else 2
    state = run_state.read_run(proj)
    assert state is not None and state["active"] is True
    assert state["queue"] == [{"todo_id": f"TODO-000{winner}", "pipeline": ""}], (
        "очередь победителя обязана остаться целой — проигравший не затирает"
    )
    refusal = results[3 - winner][1]
    assert "force=true" in refusal, f"отказ обязан указать remedy: {refusal}"


def test_two_concurrent_run_next_launch_one_item_once(tmp_git_repo, monkeypatch):
    """A-13.2: два конкурентных run_next → элемент запускается ровно один раз.

    ``start_pipeline`` — заглушка со счётчиком: первый вызов блокируется
    на gate (поток остаётся внутри окна запуска), как в A-02. Барьер —
    в обёртке ``run_state.update_run`` (оба обязаны дойти до записи).
    Baseline (advance проверяет только ``active``) — два запуска
    (красный). Фикс — один запуск, индекс +1, проигравший — отказ.
    """
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    arrivals: dict = {"n": 0}
    monkeypatch.setattr(
        run_state_mod, "update_run", _barrier_update_run(run_state_mod.update_run, arrivals)
    )

    calls: dict = {"n": 0}
    calls_lock = threading.Lock()
    release = threading.Event()

    def fake_start(project_dir, **kw):
        with calls_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            release.wait(timeout=30)
        return api_pipeline.StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    results: dict = {}

    def worker(key: str) -> None:
        results[key] = api.run_next(proj)

    t1 = threading.Thread(target=worker, args=("a",))
    t2 = threading.Thread(target=worker, args=("b",))
    t1.start()
    t2.start()

    assert _wait_until(lambda: calls["n"] >= 1), "первый run_next не дошёл до запуска"
    # Фикс: второй вызов уже отказан резервацией (его ключ в results),
    # победитель ещё внутри окна запуска. Baseline: второй дошёл до
    # запуска (calls == 2). В обоих случаях один из этих фактов есть.
    assert _wait_until(
        lambda: calls["n"] >= 2 or "a" in results or "b" in results
    ), f"второй вызов не завершился и не запустился: calls={calls['n']}, {results}"
    release.set()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), "run_next не завершились"

    assert calls["n"] == 1, (
        f"элемент очереди запущен {calls['n']} раза(ов) — конкурентные run_next "
        "дали два запуска (A-13)"
    )
    actions = {results["a"].action, results["b"].action}
    assert actions == {"started", "refused"}, (
        f"ожидался один started и один отказ, получили {actions}"
    )
    state = run_state.read_run(proj)
    assert state["index"] == 1, f"индекс обязан сдвинуться ровно на 1, получено {state['index']}"
    assert state["current"] == "TODO-0001"
    assert state["active"] is True


def test_single_start_next_status_finish_with_generation(tmp_git_repo, monkeypatch):
    """Регресс A-13.3: одиночный поток start → next → status → finish
    работает как раньше; поколение пишется (1) и переживает finish.
    Повторный старт после finish получает новое поколение (2)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")

    captured: dict = {}

    def fake_start(project_dir, **kw):
        captured.update(kw)
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    start_result = api.run_start(proj, queue=["TODO-0001"])
    assert start_result.active is True
    state = run_state.read_run(proj)
    assert state["generation"] == 1
    assert state["index"] == 0 and state["current"] == ""

    next_result = api.run_next(proj)
    assert next_result.action == "started"
    state = run_state.read_run(proj)
    assert state["index"] == 1 and state["current"] == "TODO-0001"
    assert state["generation"] == 1

    status = api.run_status(proj)
    assert status.active is True and status.index == 1

    finish = api.run_finish(proj, reason="owner asked")
    assert finish.active is False
    state = run_state.read_run(proj)
    assert state["active"] is False
    assert state["generation"] == 1, "finish не меняет поколение забега"

    # новый забег после закрытого — следующее поколение
    api.run_start(proj, queue=["TODO-0002"])
    assert run_state.read_run(proj)["generation"] == 2


def test_force_replace_still_works_and_bumps_generation(tmp_git_repo):
    """Регресс A-13.3: force=True продолжает перезаписывать активный
    забег (ранее закреплённое поведение) — и поднимает поколение."""
    proj = _project(tmp_git_repo)
    api.run_start(proj, queue=["TODO-0001"])

    result = api.run_start(proj, queue=["TODO-0002"], force=True)

    assert "previous run replaced" in result.message
    state = run_state.read_run(proj)
    assert state["queue"] == [{"todo_id": "TODO-0002", "pipeline": ""}]
    assert state["generation"] == 2
    assert state["index"] == 0 and state["current"] == ""


def test_second_start_refused_while_active(tmp_git_repo):
    """Регресс A-13.1 (последовательный вариант): активный забег — второй
    старт с force=False получает отказ, состояние не меняется."""
    proj = _project(tmp_git_repo)
    api.run_start(proj, queue=["TODO-0001"])
    before = run_state.read_run(proj)

    try:
        api.run_start(proj, queue=["TODO-0002"])
        raised = False
    except api.AwfApiError as exc:
        raised = True
        refusal = str(exc)

    assert raised, "второй старт при активном забеге обязан быть отказан"
    assert "force=true" in refusal
    after = run_state.read_run(proj)
    assert after["queue"] == before["queue"], "отказанный старт не трогает состояние"
    assert after["generation"] == before["generation"]


def test_legacy_state_without_generation_still_launches(tmp_git_repo, monkeypatch):
    """Регресс A-13.3 (суммаварийность): старый run.yaml без поля
    поколения (отсутствие = 0) читается и запускается; первый условный
    переход проходит по нулевому поколению."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    # состояние, написанное до введения поколения: поля без generation
    run_state.write_run(proj, active=True, queue=["TODO-0001"], index=0, current="")
    assert "generation" not in run_state.read_run(proj)

    captured: dict = {}

    def fake_start(project_dir, **kw):
        captured.update(kw)
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    result = api.run_next(proj)

    assert result.action == "started"
    assert captured.get("todo_id") == "TODO-0001"
    state = run_state.read_run(proj)
    assert state["index"] == 1 and state["current"] == "TODO-0001"
