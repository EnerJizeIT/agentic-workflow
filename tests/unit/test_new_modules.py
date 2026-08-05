"""Tests for new modules after A6 refactor: _env, _log, signal_watch, xdg."""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import xdg
from awf._env import awf_subprocess_env
from awf._log import log as _log
from awf.signal_watch import run_subprocess_until_signal

# ── _env.py ───────────────────────────────────────────────────────────────────


class TestAwfSubprocessEnv:

    def test_includes_permission_override(self):
        env = awf_subprocess_env()
        assert "OPENCODE_CONFIG_CONTENT" in env
        import json
        cfg = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        assert cfg["permission"]["edit"] == "allow"
        assert cfg["permission"]["bash"] == "allow"
        assert cfg["permission"]["write"] == "allow"

    def test_strips_opencode_server_env(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER", "http://localhost:8080")
        monkeypatch.setenv("OPENCODE_HOST", "http://example.com")
        env = awf_subprocess_env()
        assert "OPENCODE_SERVER" not in env
        assert "OPENCODE_HOST" not in env

    def test_strips_opencode_server_url_token(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://x")
        monkeypatch.setenv("OPENCODE_SERVER_TOKEN", "secret")
        env = awf_subprocess_env()
        assert "OPENCODE_SERVER_URL" not in env
        assert "OPENCODE_SERVER_TOKEN" not in env

    def test_preserves_unrelated_env(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_STATUS", "running")  # not in strip list
        monkeypatch.setenv("MY_CUSTOM_VAR", "value")
        env = awf_subprocess_env()
        assert env["MY_CUSTOM_VAR"] == "value"
        # A6 audit fix: explicit whitelist — STATUS NOT stripped (was wide prefix)
        assert env.get("OPENCODE_SERVER_STATUS") == "running"

    def test_preserves_path(self):
        env = awf_subprocess_env()
        assert "PATH" in env


# ── _log.py ───────────────────────────────────────────────────────────────────


class TestLog:

    def test_creates_log_file(self, tmp_path):
        logs_dir = tmp_path / "logs"
        _log(logs_dir, "test message")
        assert (logs_dir / "orchestrator.log").is_file()
        content = (logs_dir / "orchestrator.log").read_text()
        assert "test message" in content

    def test_appends_to_existing(self, tmp_path):
        logs_dir = tmp_path / "logs"
        _log(logs_dir, "first")
        _log(logs_dir, "second")
        content = (logs_dir / "orchestrator.log").read_text()
        assert "first" in content
        assert "second" in content
        assert content.count("\n") == 2

    def test_creates_logs_dir_if_missing(self, tmp_path):
        logs_dir = tmp_path / "deeply" / "nested" / "logs"
        _log(logs_dir, "creates parents")
        assert logs_dir.is_dir()

    def test_silent_on_oserror(self, tmp_path, monkeypatch):
        """No exception if write fails (e.g. permission denied)."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        def fail(*args, **kwargs):
            raise OSError("simulated")

        monkeypatch.setattr("builtins.open", fail)
        # Should not raise
        _log(logs_dir, "ignored")


# ── xdg.py ────────────────────────────────────────────────────────────────────


class TestXDG:

    def test_xdg_config_home_default(self, monkeypatch):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert xdg.xdg_config_home() == fake_home / ".config"

    def test_xdg_config_home_env(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
        assert xdg.xdg_config_home() == Path("/custom/xdg")

    def test_xdg_config_home_empty_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "")
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert xdg.xdg_config_home() == fake_home / ".config"

    def test_xdg_config_home_whitespace_env_uses_default(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "   ")
        fake_home = Path("/fake/home")
        monkeypatch.setattr(Path, "home", lambda: fake_home)
        assert xdg.xdg_config_home() == fake_home / ".config"

    def test_awf_roles_dir_uses_xdg(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom")
        assert xdg.awf_roles_dir() == Path("/custom/awf/roles")

    def test_opencode_config_file_uses_xdg(self, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", "/custom")
        assert xdg.opencode_config_file() == Path("/custom/opencode/opencode.json")


# ── signal_watch.py ───────────────────────────────────────────────────────────


class _FakeCompletedProc:
    def __init__(self, cmd, returncode):
        self.args = cmd
        self.returncode = returncode


class _ImmediateExitPopen:
    """Popen stub that exits immediately with code 0."""
    def __init__(self, cmd, cwd=None, env=None, **kwargs):
        self.cmd = cmd
        self.returncode = 0
        self.pid = 12345

    def poll(self):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode


class TestRunSubprocessUntilSignal:

    def test_exits_naturally_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("subprocess.Popen", _ImmediateExitPopen)
        result = run_subprocess_until_signal(
            cmd=["echo", "hi"], cwd=tmp_path, watch_paths=[], logs_dir=None,
        )
        assert result.returncode == 0

    def test_hard_timeout_raises(self, tmp_path, monkeypatch):
        class _Hanging:
            def __init__(self, *a, **kw):
                self.pid = 1
            def poll(self):
                return None
            def kill(self):
                pass
            def wait(self, timeout=None):
                return None

        monkeypatch.setattr("subprocess.Popen", _Hanging)
        monkeypatch.setattr("time.sleep", lambda *_: None)
        with pytest.raises(TimeoutError):
            run_subprocess_until_signal(
                cmd=["x"], cwd=tmp_path, watch_paths=[], logs_dir=None,
                hard_timeout=0,  # immediate timeout
            )
