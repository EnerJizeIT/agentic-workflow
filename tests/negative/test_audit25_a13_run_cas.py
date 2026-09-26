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

TODO-0103 (волна 6.2, флейк под xdist): флейк — не тест, а реальная дыра
A-13. Pre-read позднего вызова ``run_next`` может упасть ПОСЛЕ записи
резервации раннего: его ``cur`` уже равен ``next_id``, и CAS-кортеж позднего
вызова совпадает с состоянием на диске — «резерв» проходит второй раз,
элемент запускается дважды. Фикс — исключительный резерв: коллизия
(``cur == next_id``) разрешается по живости пайплайна
(``awf.api._liveness.resolve``): жив → отказ, мёртв → перехват мёртвого
резерва (крах между резервацией и запуском не запирает очередь).
- test_late_pre_read_after_reservation_refuses_when_pipeline_live — опасный
  интерливинг детерминирован (репро-скрипт перенесён в тест): A внутри окна
  запуска, B стартует после резервации A → ровно 1 запуск + 1 отказ; на
  до-фикс коде красный (2 запуска) — prove_red
- test_dead_reservation_taken_over_and_launched_once — регресс пробы QA B
  (TODO-0082): мёртвая резервация → повтор завершает элемент ровно один раз
Барьер теста run_next перевешен с ``update_run`` (мёртвый: ``run_next``
не ходит через ``update_run``) на реальный seam ``update_run_cas`` — ложная
детерминированность барьера и была источником флейка.
"""
from __future__ import annotations

import os
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


def _barrier_update_run_cas(real_update_run_cas, arrivals: dict, timeout: float = 15.0):
    """Двухсторонний барьер на ПЕРВОМ ``update_run_cas`` каждого потока.

    ``run_next`` не ходит через ``update_run`` — его реальный seam
    условной записи ``update_run_cas`` (резерв → commit → release).
    Барьер на ``update_run`` (прежний вариант в этом файле) в тесте
    run_next никогда не срабатывал — ложная детерминированность: исход
    зависел от расписания потоков, а не от кода (причина флейка TODO-0103).

    Оба конкурентных run_next обязаны дойти до резервации, причём с уже
    сделанными pre-read и ни одной записью резервации (барьер стоит ДО
    условной записи). Первый поток ждёт второго, второй отпускает обоих.
    Поздние CAS-вызовы того же потока (commit/release) не гейтятся.
    Таймаут — защита сьюта: если второй вызов так и не пришёл, первый
    идёт дальше, а assert на ``arrivals["seen"]`` ловит несоответствие.
    """
    lock = threading.Lock()
    both = threading.Event()
    seen: set = set()
    arrivals["seen"] = seen

    def wrapped(project_dir, mutator, **kw):
        ident = threading.get_ident()
        with lock:
            first_here = ident not in seen
            if first_here:
                seen.add(ident)
            wait = first_here and len(seen) == 1
        if wait:
            both.wait(timeout=timeout)
        elif first_here:
            both.set()
        return real_update_run_cas(project_dir, mutator, **kw)

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
    в обёртке ``run_state.update_run_cas`` (реальный seam ``run_next``;
    оба обязаны дойти до резервации, pre-read обоих — до записей).
    TODO-0103: прежний барьер на ``update_run`` был мёртв — ``run_next``
    через ``update_run`` не ходит, и тест проходил/падал по расписанию
    потоков (флейк). Baseline (advance проверяет только ``active``) —
    два запуска (красный). Фикс — один запуск, индекс +1, проигравший —
    отказ.
    """
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    arrivals: dict = {"n": 0}
    monkeypatch.setattr(
        run_state_mod,
        "update_run_cas",
        _barrier_update_run_cas(run_state_mod.update_run_cas, arrivals),
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

    assert len(arrivals.get("seen", ())) == 2, (
        f"оба run_next обязаны дойти до условной записи резервации, дошли "
        f"{len(arrivals.get('seen', ()))} — барьер не сработал, тест проверил не то"
    )
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


def test_late_pre_read_after_reservation_refuses_when_pipeline_live(tmp_git_repo, monkeypatch):
    """TODO-0103 (A-13 hole): поздний pre-read видит позицию уже зарезервированной.

    Опасный интерливинг (репро-скрипт перенесён в тест): A резервирует и
    входит в окно запуска — заглушка ``start_pipeline`` пишет PID-файл с
    живым «нашим» pid (как production, где background-лаунчер
    регистрирует pid до возврата) и ждёт gate. B стартует ПОСЛЕ резервации
    A: его pre-read видит ``current=TODO-0001``. Без проверки живости
    CAS-кортеж B совпадает с состоянием на диске и элемент запускается
    второй раз (красный: calls == 2). Фикс — коллизия ``cur == next_id``
    разрешается по ``awf.api._liveness.resolve``: пайплайн жив → B
    отказан, запуск ровно один.
    """
    from awf.api import _liveness

    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    # строгая идентификация живости на Linux читает /proc/<pid>/cmdline —
    # зашиваем seam так же, как в test_audit25_a02_lease.py: наш argv
    monkeypatch.setattr(
        _liveness, "read_cmdline", lambda pid: "python3\x00-m\x00awf\x00start\x00"
    )
    pid_file = proj / ".agentic" / "logs" / "awf-start.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    calls: dict = {"n": 0}
    calls_lock = threading.Lock()
    release = threading.Event()

    def fake_start(project_dir, **kw):
        with calls_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            # production-порядок: pid регистрируется ДО возврата из
            # start_pipeline — B, вошедший в окно после резервации, видит
            # живой пайплайн (не «мёртвый резерв»)
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
            release.wait(timeout=30)
        return api_pipeline.StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    results: dict = {}

    def worker(key: str) -> None:
        results[key] = api.run_next(proj)

    t_a = threading.Thread(target=worker, args=("a",))
    t_a.start()
    # A зарезервирован И внутри окна запуска (PID-файл записан)
    assert _wait_until(
        lambda: calls["n"] >= 1
        and pid_file.is_file()
        and (run_state.read_run(proj) or {}).get("current") == "TODO-0001"
    ), "A не дошёл до резервации + окна запуска"
    # B стартует ПОЗДНО: pre-read видит зарезервированную позицию
    t_b = threading.Thread(target=worker, args=("b",))
    t_b.start()
    t_b.join(timeout=60)
    release.set()
    t_a.join(timeout=60)
    assert not t_a.is_alive() and not t_b.is_alive(), "run_next не завершились"

    assert calls["n"] == 1, (
        f"элемент очереди запущен {calls['n']} раза(ов) — поздний pre-read "
        "увидел чужой резерв как свободную позицию (A-13 hole, TODO-0103)"
    )
    actions = {results["a"].action, results["b"].action}
    assert actions == {"started", "refused"}, (
        f"ожидался один started и один отказ, получили {actions}"
    )
    loser = "a" if results["a"].action == "refused" else "b"
    assert "reserved" in results[loser].message
    state = run_state.read_run(proj)
    assert state["index"] == 1
    assert state["current"] == "TODO-0001"
    assert state["active"] is True


def test_dead_reservation_taken_over_and_launched_once(tmp_git_repo, monkeypatch):
    """TODO-0103 (A-13 hole, регресс пробы QA B, TODO-0082): мёртвый резерв.

    Крах между резервацией и запуском оставляет на диске
    ``current=TODO-0001`` без живого пайплайна. Повторный ``run_next``
    обязан перехватить мёртвый резерв и завершить элемент ровно один раз —
    плоский «отказ когда cur == next_id» (отклонённый вариант из BLOCKED)
    запер бы очередь навсегда.
    """
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    # A зарезервировал и умер до запуска: на диске current=TODO-0001,
    # PID-файла нет, пайплайна нет
    run_state.write_run(proj, current="TODO-0001")
    assert not (proj / ".agentic" / "logs" / "awf-start.pid").is_file()

    calls: dict = {"n": 0}

    def fake_start(project_dir, **kw):
        calls["n"] += 1
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    result = api.run_next(proj)

    assert result.action == "started"
    assert calls["n"] == 1
    state = run_state.read_run(proj)
    assert state["index"] == 1
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
