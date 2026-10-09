"""TODO-0181: MCP server fault log — lifecycle and crash traces in a file.

Red on baseline: ``agent_workflow_ui.fault_log`` does not exist yet, so the
whole module fails to import.
"""
from __future__ import annotations

import os
import sys
import threading

import pytest

import agent_workflow_ui
from agent_workflow_ui import fault_log


@pytest.fixture(autouse=True)
def _clean_fault_log():
    """Forget the active log so each test starts from a fresh setup."""
    fault_log._reset()
    yield
    fault_log._reset()


class TestSetup:
    def test_env_path_creates_start_line(self, tmp_path, monkeypatch):
        log = tmp_path / "mcp-server.log"
        monkeypatch.setenv("AWF_MCP_LOG", str(log))

        result = fault_log.setup_fault_log()

        assert result == log
        assert log.is_file()
        text = log.read_text(encoding="utf-8")
        assert "START" in text
        assert f"pid={os.getpid()}" in text
        assert agent_workflow_ui.__version__ in text

    def test_explicit_path_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWF_MCP_LOG", str(tmp_path / "env.log"))

        fault_log.setup_fault_log(log_path=tmp_path / "arg.log")

        assert (tmp_path / "arg.log").is_file()
        assert not (tmp_path / "env.log").exists()

    def test_default_path_uses_xdg_state_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("AWF_MCP_LOG", raising=False)

        fault_log.setup_fault_log()

        expected = tmp_path / "awf" / "mcp-server.log"
        assert expected.is_file()

    def test_default_path_fallback_local_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        monkeypatch.delenv("AWF_MCP_LOG", raising=False)

        fault_log.setup_fault_log()

        expected = tmp_path / ".local" / "state" / "awf" / "mcp-server.log"
        assert expected.is_file()

    def test_junk_path_does_not_raise(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")

        # parent is a file → mkdir fails → setup must swallow it
        result = fault_log.setup_fault_log(log_path=blocker / "mcp-server.log")

        assert result == blocker / "mcp-server.log"
        assert not (blocker / "mcp-server.log").exists()

    def test_idempotent_second_call(self, tmp_path):
        log = tmp_path / "mcp-server.log"
        fault_log.setup_fault_log(log_path=log)
        fault_log.setup_fault_log()

        assert log.read_text(encoding="utf-8").count("START") == 1


class TestCrashHooks:
    def test_excepthook_writes_traceback(self, tmp_path, capsys):
        log = tmp_path / "mcp-server.log"
        fault_log.setup_fault_log(log_path=log)

        try:
            raise ValueError("boom")
        except ValueError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)

        text = log.read_text(encoding="utf-8")
        assert "CRASH" in text
        assert "Traceback (most recent call last):" in text
        assert "ValueError: boom" in text
        # default stderr behavior is preserved (the hook chains on)
        assert "ValueError: boom" in capsys.readouterr().err

    def test_thread_excepthook_writes_traceback(self, tmp_path):
        log = tmp_path / "mcp-server.log"
        fault_log.setup_fault_log(log_path=log)

        def boom():
            raise RuntimeError("thread boom")

        thread = threading.Thread(target=boom)
        thread.start()
        thread.join()

        text = log.read_text(encoding="utf-8")
        assert "THREAD-CRASH" in text
        assert "RuntimeError: thread boom" in text

    def test_stop_line_on_atexit_emulation(self, tmp_path):
        log = tmp_path / "mcp-server.log"
        fault_log.setup_fault_log(log_path=log)

        # emulate the atexit callback
        fault_log._stop_line(log)

        text = log.read_text(encoding="utf-8")
        assert "STOP" in text
        assert text.index("START") < text.index("STOP")


class TestRotation:
    def test_over_limit_rotates_to_one(self, tmp_path, monkeypatch):
        log = tmp_path / "mcp-server.log"
        monkeypatch.setattr(fault_log, "MAX_BYTES", 64)
        fault_log.setup_fault_log(log_path=log)  # START line already > 64 bytes

        fault_log._write(log, "A" * 80)

        rotated = tmp_path / "mcp-server.log.1"
        assert rotated.is_file()
        assert "START" in rotated.read_text(encoding="utf-8")
        assert "A" * 80 in log.read_text(encoding="utf-8")

    def test_under_limit_no_rotation(self, tmp_path):
        log = tmp_path / "mcp-server.log"
        fault_log.setup_fault_log(log_path=log)

        fault_log._write(log, "small line")

        assert not (tmp_path / "mcp-server.log.1").exists()
