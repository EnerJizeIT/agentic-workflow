"""V-01/V-02 (повторная проверка аудита 2026-09-27, 12-recovery-reliability):
отказные переходы ``run_next``.

Дефекты (воспроизведены в аудите на временных проектах):
- V-01 (P1 для foreground API): foreground-запуск с ненулевым исхом
  возвращает ``StartResult(run_mode="foreground", exit_code!=0)``; отказом
  считались только режимы ``noop/error`` — ``run_next`` фиксировал
  ``index+1``/``current`` и отвечал ``started`` для пайплайна, который
  завершился с ошибкой (аудит: outcome=started, index=1, current, .ready).
- V-02 (P2): вызов ``start_pipeline`` не защищён try/except — исключение
  летело наружу из ``run_next``, оставив созданный ``.ready`` и
  резервацию ``current`` (аудит: исключение наружу, index=0,
  current='TODO-0001', .ready=True).

Инварианты (TODO-0123):
1. Успех запуска — единое условие: background — процесс реально запущен;
   foreground — запуск ЗАВЕРШИЛСЯ успешно (exit_code == 0); всё остальное
   — отказ.
2. На любом отказном выходе (noop/error/foreground-fail/исключение)
   ``run_next`` откатывает только собственные побочные эффекты этого
   вызова: созданный им ``.ready`` удаляется, резервация index/current
   снимается; предсуществующие сигналы не трогаются.
3. Повторный ``run_next`` после любого отказа запускает тот же элемент
   обычным успешным запуском.

Findings covered:
- test_foreground_failure_does_not_advance — V-01, prove_red
- test_launch_exception_rolls_back_own_effects — V-02, prove_red
- test_retry_after_both_failure_modes_launches_once — инвариант 3
- test_foreground_failure_keeps_preexisting_ready — инвариант 2,
  вторая половина (предсуществующий сигнал не трогается)
- test_concurrent_run_next_still_one_owner — A-13: конкурентность и
  idempotency существующих путей не ослаблены новым условием
- test_reused_pid_file_is_not_a_running_pipeline — TODO-0153: pid из
  PID-файла, переиспользованный живым awf-процессом ДРУГОГО проекта,
  не считается нашим пайплайном (argv-идентичность + --project-dir)
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import awf.api.pipeline as api_pipeline
from awf import api, run_state
from awf.api._results import StartResult


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="V01V02FailureTransitions")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _ready_path(proj: Path, todo_id: str = "TODO-0001") -> Path:
    return proj / ".agentic" / "inbox" / f"{todo_id}.ready"


def _wait_until(predicate, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _assert_state_unchanged(proj: Path, before: dict) -> None:
    """Инвариант 2: отказ не двигает очередь — индекс, текущий элемент
    (резервация откатилась), поколение и состав очереди как до run_next."""
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


def test_foreground_failure_does_not_advance(tmp_git_repo, monkeypatch):
    """V-01: foreground-запуск завершился с ненулевым исходом — это отказ,
    а не старт: ответ ``refused``, индекс/current не сдвинуты, ``.ready``,
    созданный этим вызовом, не остаётся. Baseline: ``started`` + index=1
    (красный)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists(), "исходно сигнала быть не должно"

    def fake_start(project_dir, **kw):
        # production-форма foreground-исхода: run_pipeline вернул 1
        # (pipeline.py: StartResult(run_mode="foreground", exit_code=1))
        assert kw.get("background") is False
        return StartResult(
            run_mode="foreground", run_id=None, log_file=None,
            exit_code=1, message="Pipeline completed with exit code 1",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    before = run_state.read_run(proj)

    result = api.run_next(proj, background=False)

    assert result.action == "refused", (
        f"foreground с ненулевым исходом обязан быть отказом, "
        f"получено {result.action!r}: {result.message}"
    )
    assert "TODO-0001" in result.todo_id
    assert not ready.exists(), (
        "отказанный foreground-запуск обязан убрать .ready, который run_next "
        "сам создал — иначе newest_active видит активный TODO, который никуда "
        "не запущен"
    )
    _assert_state_unchanged(proj, before)


def test_launch_exception_rolls_back_own_effects(tmp_git_repo, monkeypatch):
    """V-02: исключение из ``start_pipeline`` не летит наружу из
    ``run_next`` (классифицированный отказ), созданный ``.ready`` удалён,
    резервация снята, индекс на месте. Baseline: RuntimeError наружу,
    .ready и current='TODO-0001' остались (красный)."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists()

    def fake_start(project_dir, **kw):
        raise RuntimeError("boom (fake launch crash)")

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    before = run_state.read_run(proj)

    result = api.run_next(proj)

    assert result.action == "refused", (
        f"исключение из start_pipeline обязан стать классифицированным "
        f"отказом, получено {result.action!r}: {result.message}"
    )
    assert "RuntimeError" in result.message, (
        f"отказ обязан назвать тип исключения: {result.message}"
    )
    assert not ready.exists(), (
        "крах запуска обязан удалить .ready, который run_next сам создал"
    )
    _assert_state_unchanged(proj, before)


def test_retry_after_both_failure_modes_launches_once(tmp_git_repo, monkeypatch):
    """Инвариант 3: после foreground-отказа И после краха запуска повторный
    ``run_next`` запускает тот же элемент обычным успешным запуском — ровно
    один запуск, индекс сдвинулся один раз, сигнал появился."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])
    ready = _ready_path(proj)
    assert not ready.exists()

    counter: dict = {"n": 0}

    def fake_start(project_dir, **kw):
        counter["n"] = counter.get("n", 0) + 1
        n = counter["n"]
        if n == 1:
            return StartResult(
                run_mode="foreground", run_id=None, log_file=None,
                exit_code=1, message="Pipeline completed with exit code 1",
            )
        if n == 2:
            raise RuntimeError("boom (fake launch crash)")
        return StartResult(
            run_mode="background", run_id=424242,
            log_file=str(proj / "log.txt"), exit_code=0, message="ok",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

    first = api.run_next(proj, background=False)
    assert first.action == "refused", f"первый запуск обязан быть отказом: {first.message}"
    assert not ready.exists(), "после foreground-отказа сигнала нет"

    second = api.run_next(proj)
    assert second.action == "refused", f"крах обязан быть отказом: {second.message}"
    assert not ready.exists(), "после краха сигнала нет"

    third = api.run_next(proj)

    assert third.action == "started", (
        f"повторный запуск обязан пройти: {third.message}"
    )
    assert third.todo_id == "TODO-0001", "повтор запускает тот же элемент"
    assert counter["n"] == 3, f"ровно три вызова start_pipeline, было {counter['n']}"
    assert ready.exists(), "сигнал появляется с успешным запуском"
    state = run_state.read_run(proj)
    assert state["index"] == 1, "индекс сдвинулся ровно один раз"
    assert state["current"] == "TODO-0001"
    assert state["generation"] == 1, "отказы не меняют поколение"


def test_foreground_failure_keeps_preexisting_ready(tmp_git_repo, monkeypatch):
    """Инвариант 2 (вторая половина): ``.ready``, существовавший ДО
    run_next (dispatch / ручной touch), foreground-отказом не
    уничтожается — и содержимое остаётся прежним."""
    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    ready = _ready_path(proj)
    ready.write_text("dispatch\n", encoding="utf-8")
    api.run_start(proj, queue=["TODO-0001"])

    def fake_start(project_dir, **kw):
        return StartResult(
            run_mode="foreground", run_id=None, log_file=None,
            exit_code=2, message="Pipeline completed with exit code 2",
        )

    monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)
    before = run_state.read_run(proj)

    result = api.run_next(proj, background=False)

    assert result.action == "refused"
    assert ready.exists(), "ранее существовавший .ready отказом не уничтожается"
    assert ready.read_text(encoding="utf-8") == "dispatch\n", (
        "содержимое предсуществующего .ready не меняется"
    )
    _assert_state_unchanged(proj, before)


def test_reused_pid_file_is_not_a_running_pipeline(tmp_git_repo):
    """TODO-0153 (PR #28 CI, e2e сценарий 3): PID из PID-файла переиспользован
    живым процессом ДРУГОГО проекта — его argv как раз ``python -m awf start
    --project-dir <ЧУЖОЙ проект>`` (в CI параллельные xdist-воркеры запускают
    собственные фоновые пайплайны). Baseline: resolve вернул
    ``(True, pid, "pid_file")`` — argv-идентичность совпала — и ``run_revise``
    отказал «a stage is running (source: pid_file)» (красный). После фикса:
    чужой живой awf-процесс чужого проекта — НЕ наш пайплайн, stale-файл
    убран, ревизия проходит."""
    import subprocess
    import sys

    from awf.api import _liveness

    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    # Реальный живой чужой процесс: пайплайн ДРУГОГО проекта. Каталог чужого
    # проекта существует — как в CI, где второй прогон живёт рядом.
    other = tmp_git_repo / "other-project"
    other.mkdir(exist_ok=True)
    foreign = subprocess.Popen(
        [
            sys.executable, "-c", "import time; time.sleep(120)",
            "-m", "awf", "start", "--project-dir", str(other),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        # Sanity: процесс жив, и его argv выглядит как настоящий awf start —
        # именно поэтому baseline его за наш пайплайн принимал.
        assert _liveness.probe_alive(foreign.pid)
        assert _liveness.cmdline_is_ours(_liveness.read_cmdline(foreign.pid))

        pid_file = proj / ".agentic" / "logs" / "awf-start.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(f"{foreign.pid}\n", encoding="utf-8")

        running, pid, source = _liveness.resolve(proj)
        assert (running, pid, source) == (False, None, None), (
            f"PID-файл указывает на живой awf-процесс ДРУГОГО проекта — "
            f"это не наш пайплайн: {(running, pid, source)}"
        )
        assert not pid_file.is_file(), "stale PID file обязан быть убран"

        # Сами CI-симптом: run_revise не обязан отказать по живости.
        result = api.run_revise(
            proj,
            queue=[{"todo_id": "TODO-0001", "pipeline": "default"}],
            reason="reused-pid regression", key="reused-pid-153",
        )
        assert result.action == "applied", (
            f"ревизия обязана пройти при чужом живом pid в файле: {result.message}"
        )
    finally:
        foreign.kill()
        foreign.wait()


def test_concurrent_run_next_still_one_owner(tmp_git_repo, monkeypatch):
    """A-13 (регресс): два конкурентных run_next — один владелец, при новом
    отказном условии семантика не ослаблена: победитель регистрирует
    живой пайплайн (PID-файл, как production), поздний вызов отклоняется
    по живости; запуск ровно один, индекс сдвинулся один раз."""
    from awf.api import _liveness

    proj = _project(tmp_git_repo)
    _write_todo(proj, "TODO-0001")
    api.run_start(proj, queue=["TODO-0001"])

    # строгая идентификация живости на Linux читает /proc/<pid>/cmdline —
    # зашиваем seam так же, как в test_audit25_a13_run_cas.py: наш argv
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
        # production-порядок: pid регистрируется ДО возврата из
        # start_pipeline — конкурент, вошедший в окно после резервации,
        # видит живой пайплайн
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        release.wait(timeout=30)
        return StartResult(
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
    # победитель вошёл в окно запуска (резервация закоммичена, PID-файл
    # записан); второй обязан отклониться по резервации или по живости
    assert _wait_until(lambda: pid_file.is_file()), "никто не вошёл в окно запуска"
    release.set()
    t1.join(timeout=60)
    t2.join(timeout=60)
    assert not t1.is_alive() and not t2.is_alive(), "run_next не завершились"

    assert calls["n"] == 1, (
        f"элемент очереди запущен {calls['n']} раза(ов) — конкурентные "
        "run_next дали два запуска (A-13)"
    )
    assert set(results) == {"a", "b"}, f"оба вызова обязаны завершиться: {results}"
    actions = {results["a"].action, results["b"].action}
    assert actions == {"started", "refused"}, (
        f"ожидался один started и один отказ, получили {actions}"
    )
    state = run_state.read_run(proj)
    assert state["index"] == 1, "индекс обязан сдвинуться ровно на 1"
    assert state["current"] == "TODO-0001"
    assert state["active"] is True
