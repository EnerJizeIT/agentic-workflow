"""Unit tests for awf.cmd_start — specifically background-mode argv handling."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awf import cmd_start


def _make_args(**overrides) -> SimpleNamespace:
    defaults = {
        "command": "start",
        "project_dir": ".",
        "background": True,
        "auto": False,
        "pipeline": "default",
        "from_stage": None,
        "timeout": 3600,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_background_implies_auto(tmp_path: Path) -> None:
    """--background must add --auto to child argv (stdin=DEVNULL, no input())."""
    args = _make_args(project_dir=str(tmp_path))
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            captured["kwargs"] = kwargs
            self.pid = 12345

    with patch.object(cmd_start.subprocess, "Popen", _FakeProc):
        cmd_start._run_in_background(args)

    child_argv = captured["argv"]
    assert "--auto" in child_argv, f"--auto missing from {child_argv}"
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL
    assert captured["kwargs"].get("start_new_session") is True


def test_background_preserves_explicit_auto(tmp_path: Path) -> None:
    """If user passed --auto explicitly in sys.argv, child has exactly one --auto."""
    args = _make_args(project_dir=str(tmp_path))
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            self.pid = 1

    with patch.object(cmd_start.sys, "argv", ["awf", "start", "--background", "--auto"]):
        with patch.object(cmd_start.subprocess, "Popen", _FakeProc):
            cmd_start._run_in_background(args)

    child_argv = captured["argv"]
    auto_count = child_argv.count("--auto")
    assert auto_count == 1, f"Expected exactly 1 --auto, got {auto_count} in {child_argv}"


def test_background_short_auto_flag_respected(tmp_path: Path) -> None:
    """If user used -a (short form), no extra --auto added."""
    args = _make_args(project_dir=str(tmp_path))
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            self.pid = 1

    with patch.object(cmd_start.sys, "argv", ["awf", "start", "--background", "-a"]):
        with patch.object(cmd_start.subprocess, "Popen", _FakeProc):
            cmd_start._run_in_background(args)

    child_argv = captured["argv"]
    assert "--auto" not in child_argv, f"--auto should not be added when -a present: {child_argv}"
    assert "-a" in child_argv
