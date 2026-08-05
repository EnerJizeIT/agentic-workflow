"""Unit tests for awf.cmd_start — background-mode behavior (BD-30).

After MCP-MIGRATION unification: cmd_start.run delegates to
api.start_pipeline(background=True). Tests verify BEHAVIOR (BD-30
invariant: --background does NOT force --auto), not argv reconstruction
internals (those moved to api._start_in_background).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from awf import api, cmd_start


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


def _setup_project(tmp_path: Path) -> Path:
    """Minimal .agentic/ structure required by api.start_pipeline."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic").mkdir()
    (proj / ".agentic" / "config.yaml").write_text(
        'project:\n  name: test\n'
    )
    # Dogfood-10: start_pipeline background guard requires active TODO
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(exist_ok=True)
    (inbox / "TODO-0001.ready").touch()
    (inbox / "TODO-0001.md").write_text("# Task")
    return proj


def test_background_does_NOT_force_auto_bd30(tmp_path: Path) -> None:
    """BD-30: --background must NOT add --auto to child subprocess.

    Interactive mode (auto=False) waits for signal file instead of input(),
    so it works with stdin=DEVNULL. Only --auto explicitly opts into
    subprocess supervisor mode.
    """
    proj = _setup_project(tmp_path)
    args = _make_args(project_dir=str(proj), auto=False)
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            captured["kwargs"] = kwargs
            self.pid = 12345

    with patch.object(api._background.subprocess, "Popen", _FakeProc), \
         patch("awf.api.pipeline._verify_child_alive", return_value=True):
        rc = cmd_start.run(args)

    assert rc == 0
    child_argv = captured["argv"]
    assert "--auto" not in child_argv, (
        f"BD-30: --background must NOT force --auto. Got: {child_argv}"
    )
    assert captured["kwargs"].get("stdin") == subprocess.DEVNULL
    assert captured["kwargs"].get("start_new_session") is True


def test_background_preserves_explicit_auto(tmp_path: Path) -> None:
    """If user passed --auto explicitly, child has exactly one --auto."""
    proj = _setup_project(tmp_path)
    args = _make_args(project_dir=str(proj), auto=True)
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            self.pid = 1

    with patch.object(api._background.subprocess, "Popen", _FakeProc), \
         patch("awf.api.pipeline._verify_child_alive", return_value=True):
        cmd_start.run(args)

    child_argv = captured["argv"]
    auto_count = child_argv.count("--auto")
    assert auto_count == 1, f"Expected exactly 1 --auto, got {auto_count} in {child_argv}"


def test_background_strips_background_flag_from_child(tmp_path: Path) -> None:
    """BD-30 regression: child awf start must not re-recurse with --background."""
    proj = _setup_project(tmp_path)
    args = _make_args(project_dir=str(proj))
    captured: dict = {}

    class _FakeProc:
        def __init__(self, args_list, **kwargs):
            captured["argv"] = args_list
            self.pid = 1

    with patch.object(api._background.subprocess, "Popen", _FakeProc), \
         patch("awf.api.pipeline._verify_child_alive", return_value=True):
        cmd_start.run(args)

    child_argv = captured["argv"]
    assert "--background" not in child_argv
