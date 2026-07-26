"""Tests for browser.py: detection, open, error handling."""
from __future__ import annotations

import platform as platform_module
import subprocess
from unittest.mock import patch

from agent_workflow_ui import browser


def test_detect_default_returns_known_command():
    cmd = browser.detect_open_command("auto")
    assert cmd in ("xdg-open", "open", "explorer"), f"unexpected: {cmd}"


def test_detect_explicit_passthrough():
    assert browser.detect_open_command("custom-open") == "custom-open"
    assert browser.detect_open_command("/usr/bin/xdg-open") == "/usr/bin/xdg-open"


def test_detect_on_linux():
    with patch.object(platform_module, "system", return_value="Linux"):
        assert browser.detect_open_command("auto") == "xdg-open"


def test_detect_on_macos():
    with patch.object(platform_module, "system", return_value="Darwin"):
        assert browser.detect_open_command("auto") == "open"


def test_detect_on_windows():
    with patch.object(platform_module, "system", return_value="Windows"):
        assert browser.detect_open_command("auto") == "explorer"


def test_open_path_command_not_on_path():
    with patch("agent_workflow_ui.browser.shutil.which", return_value=None):
        success, msg = browser.open_path("/tmp/test.html", command="nonexistent-cmd-xyz")
        assert not success
        assert "not found" in msg or "PATH" in msg


def test_open_path_subprocess_success(tmp_path):
    target = tmp_path / "test.html"
    target.write_text("<h1>Test</h1>")

    fake_result = subprocess.CompletedProcess(
        args=["echo", str(target)],
        returncode=0,
        stdout=b"",
        stderr=b"",
    )
    with patch("agent_workflow_ui.browser.shutil.which", return_value="/usr/bin/echo"):
        with patch("agent_workflow_ui.browser.subprocess.run", return_value=fake_result) as mock_run:
            success, msg = browser.open_path(target, command="echo")
            assert success
            assert "opened" in msg.lower() or "echo" in msg
            mock_run.assert_called_once()


def test_open_path_subprocess_nonzero_exit(tmp_path):
    target = tmp_path / "test.html"
    target.write_text("<h1>Test</h1>")

    fake_result = subprocess.CompletedProcess(
        args=["false-cmd", str(target)],
        returncode=1,
        stdout=b"",
        stderr=b"some error output",
    )
    with patch("agent_workflow_ui.browser.shutil.which", return_value="/usr/bin/false-cmd"):
        with patch("agent_workflow_ui.browser.subprocess.run", return_value=fake_result):
            success, msg = browser.open_path(target, command="false-cmd")
            assert not success
            assert "1" in msg or "error" in msg.lower()


def test_open_path_subprocess_timeout(tmp_path):
    target = tmp_path / "test.html"
    target.write_text("<h1>Test</h1>")

    with patch("agent_workflow_ui.browser.shutil.which", return_value="/usr/bin/xdg-open"):
        with patch(
            "agent_workflow_ui.browser.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["xdg-open", str(target)], timeout=5),
        ):
            success, msg = browser.open_path(target, command="xdg-open")
            assert not success
            assert "timed out" in msg.lower() or "timeout" in msg.lower()


def test_open_path_subprocess_filenotfound(tmp_path):
    target = tmp_path / "test.html"
    target.write_text("<h1>Test</h1>")

    with patch("agent_workflow_ui.browser.shutil.which", return_value="/usr/bin/xdg-open"):
        with patch(
            "agent_workflow_ui.browser.subprocess.run",
            side_effect=FileNotFoundError(2, "No such file"),
        ):
            success, msg = browser.open_path(target, command="xdg-open")
            assert not success
            assert "not found" in msg.lower()
