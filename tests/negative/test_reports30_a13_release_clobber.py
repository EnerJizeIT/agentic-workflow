"""A-13 остаточный угол (REPORTS30, TODO-0189): release не затирает перехват.

Дефект (воспроизведён этим файлом): резервация позиции очереди
(``run_next``) не имела владельца. Перехват мёртвой резервации
(TODO-0103: ``cur == next_id`` + лiveness мёртв → повторная резервация)
писал ``current=next_id → next_id`` — незаметный no-op, который
нематериально передавал позицию вызову-перехватчику. После этого
``_release_reservation()`` оригинального владельца (его собственный
запуск отказан — lease A-02 держит перехвативший вызов; в
foreground живость в state появляется только после первой стадии)
сходился по CAS-кортежу ``(generation, index, current)`` — он не
идентифицирует владельца — и сбрасывал ``current`` в своё
pre-reserve значение: активная резервация перехватчика затирается.
Коммит перехватчика затем падает (CAS-промах по ``current``), забег
«теряет» элемент: индекс не двигается, элемент не зачитан — следующий
``run_next`` может запустить его второй раз (потеря защиты от
двойного захвата). Lease A-02 угол сдерживает, но не закрывает:
отказ по lease сам и есть путь затирающего release.

Инварианты:
1. Release снимает только СВОЮ резервацию: сверка владельца
   (штамп ``reserved_by`` в run.yaml, ставится при резервации;
   перехват переписывает штамп своим). Чужая/перехваченная
   резервация не трогается; семантика сдержанного lease A-02 не
   меняется (``awf/api/_lease.py`` не трогаем).
2. Обычный отказ (launch refused) по-прежнему снимает резервацию
   владельца — позиция возвращается pre-reserve значению, штамп
   очищается.
3. Orphaned-путь (run закрыт в окне запуска, перехвата нет):
   release владельца по-прежнему применяется — «закрытый забег
   владеет current» (audit05) не остаётся.

Findings covered:
- test_refused_launch_release_does_not_clobber_takeover_reservation —
  опасный интерливинг детерминирован (A зажат между резервацией и
  запуском, пока B внутри окна запуска): до-фикс release A сбрасывает
  ``current`` в "" (красный) и B уходит в orphaned; после фикса
  ``current`` цела, B коммитит индекс ровно один раз
- test_refused_launch_still_releases_own_reservation — регресс:
  одиночный отказанный запуск снимает свою резервацию
- test_orphaned_launch_release_restores_own_reservation — регресс:
  run закрыт в окне запуска без перехвата — release владельца
  применяется, позиция возвращается
"""
from __future__ import annotations

import threading
from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A13ReleaseClobber")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _fake_start_noop(proj: Path, **_kw):
    """Запуск отказан (A-02: lease держит другой живой запуск)."""
    return api_pipeline.StartResult(
        run_mode="noop",
        run_id=None,
        log_file="",
        exit_code=None,
        message="another launch is already in progress",
    )


def test_refused_launch_release_does_not_clobber_takeover_reservation(tmp_git_repo, monkeypatch):
    """A-13 остаточный угол: отказанный запуск не затирает чужую (перехваченную)
    резервацию.

    Детерминированный интерливинг (репро перенесено в тест):

    1. A: pre-read (``current=""``) → резервация (``current=TODO-0001``)
       → ждёт в baseline-гейте (зажат МЕЖДУ резервацией и запуском).
    2. B: pre-read видит ``current=TODO-0001``; liveness мёртв (PID-файла
       нет — как foreground до первой стадии) → перехват (CAS проходит,
       позиция теперь у B) → входит в окно запуска (заглушка
       ``start_pipeline`` держит его, пока гейт не отпустит).
    3. A отпускается, его запуск отказан (lease у B) → A делает
       ``_release_reservation``. До-фикс CAS сходится (кортеж без
       владельца) и ``current`` сбрасывается в "" — резервация B
       затирается (красный). После фикса сверка ``reserved_by`` промах
       — чужая резервация не трогается.
    4. B отпускается, запуск успешен → B обязан коммитнуть индекс
       ровно один раз. До-фикс его коммит падает (CAS-промах по
       ``current``) и B уходит в «orphaned»; после фикса индекс +1.
    """
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    in_start = threading.Event()  # B внутри окна запуска
    b_go = threading.Event()  # B стартует по команде
    release_b = threading.Event()  # B's start_pipeline возвращается
    ident_holder: dict = {}
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def fake_start(project_dir, **kw):
        with calls_lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            # B: внутри окна запуска; живость пайплайна ещё НЕ видна
            # (PID-файл не записан — как foreground до первой стадии)
            in_start.set()
            release_b.wait(timeout=30)
            return api_pipeline.StartResult(
                run_mode="background", run_id=1,
                log_file=str(proj / "log.txt"), exit_code=0, message="ok",
            )
        # A: запуск отказан — lease держит в-flight вызов B (A-02)
        return api_pipeline.StartResult(
            run_mode="noop", run_id=None, log_file="", exit_code=None,
            message="another launch is already in progress",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    # create_baseline — единственный seam МЕЖДУ резервацией и запуском:
    # A ждёт здесь, пока B не войдёт в окно запуска (интерливинг
    # детерминирован, не зависит от расписания потоков). B проходит
    # сразу. Заглушка no-op: baseline в run_next best-effort.
    def gated_baseline(project_dir, todo_id, **kw):
        if threading.get_ident() != ident_holder.get("b"):
            in_start.wait(timeout=30)
        return None

    monkeypatch.setattr(api_pipeline, "create_baseline", gated_baseline)

    results: dict = {}

    def worker(key: str) -> None:
        if key == "b":
            b_go.wait(timeout=30)
        results[key] = api.run_next(proj)

    t_a = threading.Thread(target=worker, args=("a",))
    t_a.start()
    # A зарезервирован и зажат в baseline-гейте (до запуска)
    assert _wait_until(
        lambda: (run_state.read_run(proj) or {}).get("current") == "TODO-0001"
    ), "A не дошёл до резервации"

    t_b = threading.Thread(target=worker, args=("b",))
    t_b.start()
    ident_holder["b"] = t_b.ident
    b_go.set()
    # B внутри окна запуска (A по-прежнему зажат)
    assert in_start.wait(timeout=30), "B не вошёл в окно запуска"

    t_a.join(timeout=60)
    assert not t_a.is_alive(), "A не завершился"

    state = run_state.read_run(proj)
    assert state["current"] == "TODO-0001", (
        f"отказанный запуск A затушиал резервацию B: current={state['current']!r} "
        "(A-13 остаточный угол — release без сверки владельца)"
    )
    assert results["a"].action == "refused", results["a"].message

    release_b.set()
    t_b.join(timeout=60)
    assert not t_b.is_alive(), "B не завершился"

    assert calls["n"] == 2, f"запусков было {calls['n']}, ожидалось 2"
    final = run_state.read_run(proj)
    assert final["index"] == 1, (
        f"индекс обязан сдвинуться ровно на 1, получено {final['index']} — "
        "коммит B упал из-за затушёванной резервации (orphaned)"
    )
    assert final["current"] == "TODO-0001"
    assert results["b"].action == "started", results["b"].message
    assert "orphaned" not in results["b"].message, (
        f"B ушёл в orphaned из-за чужого release: {results['b'].message}"
    )


def test_refused_launch_still_releases_own_reservation(tmp_git_repo, monkeypatch):
    """Регресс: одиночный отказанный запуск снимает СВОЮ резервацию.

    Перехвата нет — release владельца по-прежнему восстанавливает
    pre-reserve значение ``current`` и очищает штамп владельца.
    """
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    monkeypatch.setattr(api_pipeline, "start_pipeline", _fake_start_noop)

    result = api.run_next(proj)

    assert result.action == "refused"
    state = run_state.read_run(proj)
    assert state["index"] == 0
    assert state["current"] == "", (
        f"отказанный запуск обязан снять свою резервацию, current={state['current']!r}"
    )
    assert "reserved_by" not in state, "штамп владельца обязан очиститься с резервацией"
    assert state["active"] is True


def test_orphaned_launch_release_restores_own_reservation(tmp_git_repo, monkeypatch):
    """Регресс: run закрыт в окне запуска (перехвата нет) — release владельца
    по-прежнему применяется: «закрытый забег владеет current» (audit05)
    не остаётся, позиция возвращается pre-reserve значению."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    def fake_start(project_dir, **kw):
        # run закрыт МЕЖДУ запуском и коммитом (перехватчика нет)
        run_state.write_run(project_dir, active=False)
        return api_pipeline.StartResult(
            run_mode="background", run_id=1,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    result = api.run_next(proj)

    assert result.action == "started"
    assert "orphaned" in result.message
    state = run_state.read_run(proj)
    assert state["index"] == 0, "коммит закрытого забега не записывается"
    assert state["current"] == "", (
        f"release владельца обязан снять его резервацию, current={state['current']!r}"
    )
    assert "reserved_by" not in state
    assert state["active"] is False
