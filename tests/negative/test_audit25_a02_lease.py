"""A-02 (аудит 2026-09-25, слои 2 и 12): один владелец запуска на проект (lease).

Дефект (воспроизведён в аудите): ``start_pipeline``/``continue_pipeline``
проверяют живость и спавнят ребёнка без общей блокировки — два
одновременных ``start`` (барьер после liveness-проверки, спавн заглушкой)
дают два запуска. ``run_next`` идёт через ``start_pipeline`` — то же окно.

Инварианты:
1. В любой момент не более одного запуска на проект: одновременные
   публичные вызовы дают ровно один спавн; остальные получают внятный
   отказ — ``run_mode="noop"`` с текстом (выбор зафиксирован в доктрине
   03-launch-lease), отказ не блокирующий.
2. Мёртвый владелец не блокирует проект навсегда: staleness — живость
   процесса, а не возраст файла.
3. Жизненный цикл не изменился: после завершения владельца новый запуск
   проходит; lease с мёртвым pid перехватывается; foreground не сломан.

Реальные процессы не запускаются: Popen замокан, проект — ``tmp_git_repo``.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from awf import api


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="LeaseA02")
    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").touch()
    return tmp_git_repo


class _FakeProc:
    """Заглушка запущенного ребёнка: «жив», pid = тестовый процесс (> 1)."""

    def __init__(self) -> None:
        self.pid = os.getpid()
        self.returncode = None
        self.stdout = None
        self.stderr = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def terminate(self):
        pass

    def communicate(self, timeout=None):
        return ("", "")


def _blocking_popen(calls: dict, gate: threading.Event):
    """Первый спавн блокируется на gate (поток остаётся внутри запуска);
    каждый следующий спавн — подсчитан и возвращает сразу."""

    def fake_popen(cmd, *args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        if calls["n"] == 1:
            gate.wait(timeout=30)
        return _FakeProc()

    return fake_popen


def _counting_popen(calls: dict):
    def fake_popen(cmd, *args, **kwargs):
        calls["n"] = calls.get("n", 0) + 1
        return _FakeProc()

    return fake_popen


def _wait_until(calls: dict, n: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.get("n", 0) >= n:
            return True
        time.sleep(0.02)
    return False


def _dead_pid() -> int:
    """Детерминированно мёртвый pid: вне диапазона pid ОС, для него
    ``probe_alive`` не может вернуть True (``os.kill`` → EINVAL).

    Прежнее max(/proc)+1 было гонкой: в параллельном сьюте ОС может
    выдать этот pid чужому процессу между вычислением и проверкой в
    ``acquire`` — тест падал «another launch is already in progress»
    (QA-проход A-02, 2026-09-25: 2 падения полного сьюта на ``-n auto``).
    """
    pid = 2**31
    from awf.api import _liveness

    assert not _liveness.probe_alive(pid)
    return pid


def test_two_concurrent_starts_spawn_once(tmp_git_repo, monkeypatch):
    """Два одновременных start → ровно один спавн, второй вызов — отказ."""
    proj = _project(tmp_git_repo)
    calls: dict = {}
    gate = threading.Event()
    monkeypatch.setattr(subprocess, "Popen", _blocking_popen(calls, gate))
    monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

    first: dict = {}

    def launch():
        try:
            first["result"] = api.start_pipeline(proj, background=True)
        except BaseException as e:  # noqa: BLE001 — surfaced in the asserts
            first["error"] = e

    t = threading.Thread(target=launch)
    t.start()
    assert _wait_until(calls, 1), "первый запуск должен дойти до спавна"

    # Первый запуск ещё внутри (Popen блокируется на gate) — второй вызов
    # должен получить отказ, а не второй спавн.
    second = api.start_pipeline(proj, background=True)

    gate.set()
    t.join(timeout=30)
    assert not t.is_alive(), "первый запуск не завершился"
    assert "error" not in first, f"первый запуск упал: {first.get('error')!r}"

    assert calls.get("n", 0) == 1, (
        f"Popen вызван {calls.get('n')} раз(а) — два одновременных start "
        "дали два запуска (A-02)"
    )
    assert first["result"].run_mode == "background"
    assert second.run_id is None, "отказ не должен возвращать PID"


def test_second_start_refused_while_launch_in_progress(tmp_git_repo, monkeypatch):
    """Второй start во время первого — отказ с внятным текстом (не ожидание)."""
    proj = _project(tmp_git_repo)
    calls: dict = {}
    gate = threading.Event()
    monkeypatch.setattr(subprocess, "Popen", _blocking_popen(calls, gate))
    monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

    t = threading.Thread(target=lambda: api.start_pipeline(proj, background=True))
    t.start()
    assert _wait_until(calls, 1), "первый запуск должен дойти до спавна"

    # Main-поток получил результат ВО ВРЕМЯ первого запуска (gate ещё не
    # установлен) — если бы это было блокирующее ожидание, тест завис бы.
    second = api.start_pipeline(proj, background=True)

    gate.set()
    t.join(timeout=30)

    assert calls.get("n", 0) == 1, "второй вызов не должен спавнить"
    assert second.run_mode == "noop", (
        f"ожидался отказ (noop), получил {second.run_mode!r}: {second.message}"
    )
    assert second.run_id is None
    assert "in progress" in second.message, second.message
    assert "awf_status" in second.message, second.message


class TestLaunchLeaseRegressions:
    """Регрессы (не в prove_red): жизненный цикл вокруг lease не сломан."""

    def test_launch_passes_after_owner_finishes(self, tmp_git_repo, monkeypatch):
        """Вызов владельца вернулся (запуск завершён) — новый запуск проходит."""
        proj = _project(tmp_git_repo)
        calls: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _counting_popen(calls))
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

        first = api.start_pipeline(proj, background=True)
        assert first.run_mode == "background"

        from awf.api import _lease

        assert not _lease.lease_path(proj).exists(), (
            "lease должен быть освобождён после возврата вызова запуска"
        )
        second = api.start_pipeline(proj, background=True)
        assert second.run_mode == "background"
        assert calls.get("n", 0) == 2

    def test_dead_owner_lease_is_taken_over(self, tmp_git_repo, monkeypatch):
        """Lease с мёртвым pid — stale по живости, новый запуск перехватывает."""
        proj = _project(tmp_git_repo)
        calls: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _counting_popen(calls))
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

        from awf.api import _lease

        _lease.lease_path(proj).write_text(f"{_dead_pid()}\n", encoding="utf-8")

        result = api.start_pipeline(proj, background=True)
        assert result.run_mode == "background", (
            f"lease с мёртвым pid заблокировал проект: {result.message}"
        )
        assert calls.get("n", 0) == 1
        assert not _lease.lease_path(proj).exists()

    def test_foreground_launch_not_broken(self, tmp_git_repo, monkeypatch):
        """Foreground-запуск (background=False) работает как раньше."""
        proj = _project(tmp_git_repo)
        import awf.orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "run_pipeline", lambda args: 0)
        result = api.start_pipeline(proj, background=False)
        assert result.run_mode == "foreground"
        assert result.exit_code == 0

    def test_launch_not_broken_by_error_between_acquire_and_spawn(
        self, tmp_git_repo, monkeypatch
    ):
        """Ошибка между acquire и spawn не должна оставлять висячий lease.

        Ошибка ровно между чтением и записью (тут — _reconcile, который
        идёт сразу после acquire в continue_pipeline): вызов падает с
        исключением, но lease с ЖИВЫМ pid процесса-вызывающего остаётся
        на диске. Дальше любой новый запуск отклоняется «another launch
        is already in progress» — проект заблокирован до рестарта
        процесса (в MCP это сервер, а не короткий CLI).
        """
        proj = _project(tmp_git_repo)
        calls: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _counting_popen(calls))
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

        import awf.api.pipeline as pipeline_mod
        from awf.api import _lease

        def _boom(project_dir):
            raise RuntimeError("injected FS-style error")

        # _reconcile вызывается в continue_pipeline сразу после acquire.
        monkeypatch.setattr(pipeline_mod, "_reconcile", _boom)
        with pytest.raises(RuntimeError, match="injected"):
            api.continue_pipeline(proj, background=True)

        # Lease обязан быть освобождён: иначе живой pid блокирует проект.
        assert not _lease.lease_path(proj).exists(), (
            "lease с живым pid остался после ошибки между acquire и spawn — "
            "проект заблокирован до смерти процесса-вызывающего"
        )
        # Новый запуск после ошибки проходит.
        monkeypatch.setattr(pipeline_mod, "_reconcile", lambda project_dir: None)
        result = api.continue_pipeline(proj, background=True)
        assert result.run_mode == "background", (
            f"запуск после ошибки отклонён висячим lease: "
            f"{result.run_mode!r}: {result.message}"
        )
        assert calls.get("n", 0) == 1

    def test_taked_over_empty_lease_single_spawn(self, tmp_git_repo, monkeypatch):
        """Lease создан, pid ещё не записан (пустой файл) — второй запуск
        перехватывает его как stale (владелец нечитаем = не живой), спавнит
        и закрывает окно; первый (сталий) владелец, дописав pid в уже
        переименованный inode, упирается в liveness-проверку по PID-файлу
        перехватчика и получает отказ, а не второй спавн. Инвариант A-02
        держится в любом порядке интерливинга двух потоков: ровно один
        спавн, один background, один noop."""
        proj = _project(tmp_git_repo)
        calls: dict = {}
        gate = threading.Event()
        lease_content = f"{os.getpid()}\n".encode()
        real_write = os.write
        first_blocked: dict = {}

        def fake_write(fd, data):
            if data == lease_content and not first_blocked:
                first_blocked["yes"] = True
                gate.wait(timeout=30)
            return real_write(fd, data)

        monkeypatch.setattr(os, "write", fake_write)
        monkeypatch.setattr(subprocess, "Popen", _counting_popen(calls))
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)
        # Фейк-ребёнок = тестовый процесс; строгому резолверу живости
        # его argv («pytest») не пройти. Документированный шов
        # read_cmdline: cmdline как у настоящего `-m awf start`.
        from awf.api import _liveness

        monkeypatch.setattr(
            _liveness, "read_cmdline", lambda pid: "python3\x00-m\x00awf\x00start\x00"
        )

        results: dict = {}

        def launch(key):
            try:
                results[key] = api.start_pipeline(proj, background=True)
            except BaseException as e:  # noqa: BLE001 — surfaced in the asserts
                results[key] = e

        ta = threading.Thread(target=lambda: launch("a"))
        tb = threading.Thread(target=lambda: launch("b"))
        ta.start()
        tb.start()

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not first_blocked:
            time.sleep(0.01)
        assert first_blocked, "первая запись lease должна была заблокироваться"

        # Перехватчик завершён сам; заблокированный — ждёт gate.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            settled = [k for k in ("a", "b") if k in results]
            if len(settled) == 1:
                break
            time.sleep(0.01)
        assert len(settled) == 1, (
            f"один поток обязан завершиться до gate, завершилось "
            f"{len(settled)}: {results}"
        )

        gate.set()
        ta.join(timeout=30)
        tb.join(timeout=30)
        assert not ta.is_alive() and not tb.is_alive(), "запуски не завершились"

        for key, value in results.items():
            assert not isinstance(value, BaseException), (
                f"запуск {key} упал: {value!r}"
            )

        modes = [results["a"].run_mode, results["b"].run_mode]
        assert calls.get("n", 0) == 1, (
            f"перехват незаписанного lease дал {calls.get('n')} спавна — "
            "инвариант «ровно один спавн» (A-02)"
        )
        assert modes.count("background") == 1 and modes.count("noop") == 1, (
            f"ожидался один background (перехватчик) и один noop (сталий "
            f"владелец через liveness), получили: {modes}"
        )

        from awf.api import _lease

        assert not _lease.lease_path(proj).exists(), (
            "lease должен быть освобождён после обоих вызовов"
        )

    def test_launch_not_broken_by_error_between_acquire_and_spawn_start(
        self, tmp_git_repo, monkeypatch
    ):
        """То же окно в start_pipeline: исключение между acquire и
        start_in_background обязано освободить lease (одна конструкция
        «проверка живости → spawn» в обоих публичных входах)."""
        proj = _project(tmp_git_repo)
        calls: dict = {}
        monkeypatch.setattr(subprocess, "Popen", _counting_popen(calls))
        monkeypatch.setattr("awf.api.pipeline._verify_child_alive", lambda *a, **k: True)

        from awf.api import _lease, _liveness

        real_resolve = _liveness.resolve
        state = {"boom": True}

        def _flaky_resolve(project_dir):
            if state["boom"]:
                state["boom"] = False
                raise RuntimeError("injected error between acquire and spawn")
            return real_resolve(project_dir)

        # resolve вызывается в start_pipeline сразу после acquire.
        monkeypatch.setattr(_liveness, "resolve", _flaky_resolve)
        with pytest.raises(RuntimeError, match="injected"):
            api.start_pipeline(proj, background=True)

        assert not _lease.lease_path(proj).exists(), (
            "lease с живым pid остался после ошибки между acquire и spawn — "
            "проект заблокирован до смерти процесса-вызывающего"
        )
        result = api.start_pipeline(proj, background=True)
        assert result.run_mode == "background", (
            f"запуск после ошибки отклонён висячим lease: "
            f"{result.run_mode!r}: {result.message}"
        )
        assert calls.get("n", 0) == 1
