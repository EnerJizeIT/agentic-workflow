"""TODO-0077: background engine pinned to a checkout (automation.runner_dir).

Invariants under test:
1. `automation.runner_dir` in .agentic/config.yaml → the background child
   (`awf start` / `awf continue`) is spawned with cwd = that directory —
   the engine imports the pinned checkout, not the project tree (awf
   editing itself: `python -m awf` puts cwd first on sys.path).
2. The directory is validated BEFORE spawn: exists and contains
   `awf/__init__.py`; otherwise AwfApiError — no process, no PID file.
3. Child argv and `--project-dir` are unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from awf import api
from awf.api import _background
from awf.api._errors import AwfApiError


class _FakeProc:
    pid = 4242


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (config.yaml + supervisor.md)."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _set_runner_dir(project: Path, value) -> None:
    cfg_file = project / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    data.setdefault("automation", {})["runner_dir"] = value
    cfg_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    cfg_file.chmod(0o600)


def _fake_runner_checkout(tmp_path: Path) -> Path:
    runner = tmp_path / "runner-checkout"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text("", encoding="utf-8")
    return runner


def _record_popen(monkeypatch) -> dict:
    calls: dict = {}

    def fake_popen(argv, **kwargs):
        calls["argv"] = list(argv)
        calls.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(_background.subprocess, "Popen", fake_popen)
    return calls


def _spawn(project: Path) -> tuple:
    return _background.start_in_background(
        project, pipeline=None, from_stage=None, auto=False, timeout=60
    )


def _pid_file(project: Path) -> Path:
    return project / ".agentic" / "logs" / "awf-start.pid"


# ─── invariant 1: the key pins the child cwd ────────────────────────────


def test_runner_dir_sets_child_cwd(awf_project, tmp_path, monkeypatch):
    runner = _fake_runner_checkout(tmp_path)
    _set_runner_dir(awf_project, str(runner))
    calls = _record_popen(monkeypatch)

    _spawn(awf_project)

    assert Path(calls["cwd"]) == runner
    assert calls["argv"][:4] == [sys.executable, "-m", "awf", "start"]
    assert calls["argv"][4:6] == ["--project-dir", str(awf_project)]


def test_missing_runner_dir_key_keeps_project_cwd(awf_project, monkeypatch):
    calls = _record_popen(monkeypatch)

    _spawn(awf_project)

    assert Path(calls["cwd"]) == awf_project


# ─── invariant 2: validation before spawn ───────────────────────────────


def test_invalid_runner_dir_raises_before_spawn(awf_project, tmp_path, monkeypatch):
    _set_runner_dir(awf_project, str(tmp_path / "does-not-exist"))
    calls = _record_popen(monkeypatch)

    with pytest.raises(AwfApiError):
        _spawn(awf_project)

    assert "argv" not in calls, "Popen must not be called on invalid runner_dir"
    assert not _pid_file(awf_project).exists()


def test_runner_dir_without_init_py_raises_before_spawn(
    awf_project, tmp_path, monkeypatch
):
    not_a_checkout = tmp_path / "not-a-checkout"
    not_a_checkout.mkdir()
    _set_runner_dir(awf_project, str(not_a_checkout))
    calls = _record_popen(monkeypatch)

    with pytest.raises(AwfApiError):
        _spawn(awf_project)

    assert "argv" not in calls, "Popen must not be called on invalid runner_dir"
    assert not _pid_file(awf_project).exists()


def test_non_string_runner_dir_raises_before_spawn(awf_project, monkeypatch):
    """QA-0077: YAML `runner_dir: 42` must fail as AwfApiError, not TypeError."""
    _set_runner_dir(awf_project, 42)
    calls = _record_popen(monkeypatch)

    with pytest.raises(AwfApiError):
        _spawn(awf_project)

    assert "argv" not in calls, "Popen must not be called on invalid runner_dir"
    assert not _pid_file(awf_project).exists()
