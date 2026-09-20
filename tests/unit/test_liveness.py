"""AUD04-07: единый резолвер живости — строгая идентификация, оба источника.

До фикса живость определяли два независимых кода (state.pipeline_pid для
start/continue/kill и awf-start.pid для status) с разной строгостью
PID-проверки. Теперь: один :func:`awf.api._liveness.resolve` + одна
строгая идентификация (argv = ``... -m awf start|continue ...``).

RED-first contract: kill-тест «не заявлять успех для неубитого» падает на
пре-фикс коде (ProcessLookupError на SIGTERM трактовался как killed=True).
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading

import pytest

from awf.api import _background, _liveness
from awf.api.pipeline import _is_pipeline_running, kill_pipeline
from awf.pipeline_state import write_state

AWF_ARGV = "python\x00-m\x00awf\x00start\x00--project-dir\x00/x\x00"


@pytest.fixture
def project(tmp_git_repo):
    return tmp_git_repo


def _start_sleeper():
    """A real live python process WITHOUT awf in its argv."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestIdentity:
    """PID считается нашим только если argv = `-m awf start|continue`."""

    def test_live_python_process_without_awf_is_none(self, project):
        """Реальный живущий python-процесс без awf в argv → не наш пайплайн."""
        proc = _start_sleeper()
        try:
            write_state(project, pipeline_pid=proc.pid)
            assert _is_pipeline_running(project) is None
        finally:
            proc.kill()
            proc.wait()

    def test_our_argv_passes(self, project, monkeypatch):
        monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)
        write_state(project, pipeline_pid=os.getpid())
        assert _is_pipeline_running(project) == os.getpid()

    def test_foreign_cmdline_rejected(self, project, monkeypatch):
        monkeypatch.setattr(
            _liveness, "read_cmdline", lambda pid: "nginx\x00worker\x00"
        )
        write_state(project, pipeline_pid=os.getpid())
        assert _is_pipeline_running(project) is None

    def test_unreadable_cmdline_never_ours(self, project, monkeypatch):
        monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: None)
        write_state(project, pipeline_pid=os.getpid())
        assert _is_pipeline_running(project) is None


class TestResolverSources:
    """Один код-путь на оба источника; foreground без PID-файла виден."""

    def test_foreground_pipeline_visible_without_pid_file(self, project, monkeypatch):
        """state.pipeline_pid + живой argv → running, даже без PID-файла."""
        monkeypatch.setattr(
            _liveness, "read_cmdline",
            lambda pid: AWF_ARGV if pid == os.getpid() else None,
        )
        write_state(project, pipeline_pid=os.getpid())
        pid_file = project / ".agentic" / "logs" / "awf-start.pid"
        assert not pid_file.exists()

        running, pid, source = _liveness.resolve(project)
        assert (running, source) == (True, "state")
        assert pid == os.getpid()

        # status-путь (check_pipeline_running) видит то же
        r2, p2, _tail = _background.check_pipeline_running(project)
        assert r2 is True
        assert p2 == os.getpid()

    def test_pid_file_source(self, project, monkeypatch):
        logs = project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        pid_file = logs / "awf-start.pid"
        monkeypatch.setattr(
            _liveness, "read_cmdline",
            lambda pid: AWF_ARGV if pid == os.getpid() else None,
        )
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

        running, pid, source = _liveness.resolve(project)
        assert (running, source) == (True, "pid_file")
        assert pid == os.getpid()
        assert pid_file.is_file(), "live PID file must be kept"

    def test_foreign_pid_file_cleaned(self, project):
        """PID-файл на живущий НЕ-awf процесс → stale → файл удаляется."""
        logs = project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        pid_file = logs / "awf-start.pid"
        # pytest-процесс жив, но его argv — не `-m awf ...`
        pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")

        running, pid, source = _liveness.resolve(project)
        assert (running, pid, source) == (False, None, None)
        assert not pid_file.is_file(), "stale/foreign PID file must be cleaned"


class TestKillRace:
    """kill в конкурентной паре не заявляет успех для неубитого."""

    def test_kill_does_not_claim_success_for_already_dead(self, project, monkeypatch):
        """Процесс умер между liveness-check и нашим SIGTERM (партер выиграл
        гонку) → killed=False, не True."""
        proc = _start_sleeper()
        try:
            write_state(project, pipeline_pid=proc.pid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)
            real_kill = os.kill

            def flaky_kill(pid, sig):
                if pid == proc.pid and sig == signal.SIGTERM:
                    # процесс умирает в момент нашего «сигнала» — доставка
                    # не состоялась: чей-то сигнал успел раньше
                    real_kill(pid, signal.SIGKILL)
                    raise ProcessLookupError
                return real_kill(pid, sig)

            monkeypatch.setattr(os, "kill", flaky_kill)

            res = kill_pipeline(project)

            assert res["pid"] == proc.pid
            assert res["killed"] is False, f"false success claimed: {res}"
            assert "already exited" in res["message"]
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def test_kill_pair_parallel_pipeline_dies_once(self, project, monkeypatch):
        """Два параллельных kill: процесс умирает, состояние очищается,
        поздний kill не находит ничего."""
        proc = _start_sleeper()
        try:
            write_state(project, pipeline_pid=proc.pid)
            monkeypatch.setattr(_liveness, "read_cmdline", lambda pid: AWF_ARGV)

            results: list = []
            barrier = threading.Barrier(2)

            def worker():
                barrier.wait()
                results.append(kill_pipeline(project))

            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            t2.start()
            t1.join(timeout=30)
            t2.join(timeout=30)

            assert proc.poll() is not None, "pipeline process must be dead"
            assert len(results) == 2
            assert any(r["killed"] for r in results), f"nobody claimed the kill: {results}"

            # поздний kill — ничего не находит, и успех не заявляет
            res = kill_pipeline(project)
            assert res["killed"] is False
            assert res["pid"] is None
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
