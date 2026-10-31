"""Incident 31.10: a pinned runner behind this awf must refuse the launch.

``automation.runner_dir`` makes the background child import the pinned
checkout. A runner left at an older commit silently ran the older engine —
the freshly fixed spawn looked broken until the checkout was synced by hand.
The gate is a refusal with the exact sync command, not a warning.
"""
from __future__ import annotations

from pathlib import Path

from awf import versions
from awf.api import pipeline as api_pipeline


def _project(tmp_path: Path, runner: Path | None) -> Path:
    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True)
    config = "project:\n  name: x\n"
    if runner is not None:
        config += f"automation:\n  runner_dir: {runner}\n"
    (project / ".agentic" / "config.yaml").write_text(config, encoding="utf-8")
    return project


def _runner(tmp_path: Path) -> Path:
    runner = tmp_path / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text("", encoding="utf-8")
    return runner


def _commits(monkeypatch, pinned: str, running: str) -> None:
    monkeypatch.setattr(versions, "repo_commit", lambda path: pinned)
    monkeypatch.setattr(versions, "process_commit", lambda: running)


def test_same_commit_is_allowed(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    _commits(monkeypatch, "abc1234", "abc1234")
    assert api_pipeline._runner_drift_refusal(_project(tmp_path, runner)) is None


def test_unpinned_project_is_allowed(tmp_path, monkeypatch):
    _commits(monkeypatch, "", "")
    assert api_pipeline._runner_drift_refusal(_project(tmp_path, None)) is None


def test_drift_is_refused_with_the_sync_command(tmp_path, monkeypatch, capsys):
    runner = _runner(tmp_path)
    _commits(monkeypatch, "1111111", "2222222")

    refusal = api_pipeline._runner_drift_refusal(_project(tmp_path, runner))

    assert refusal is not None
    assert refusal.exit_code == 1
    assert "1111111" in refusal.message and "2222222" in refusal.message
    assert f'git -C "{runner}" reset --hard 2222222' in refusal.message
    assert "pinned runner drift" in capsys.readouterr().err


def test_no_commit_evidence_is_not_drift(tmp_path, monkeypatch):
    runner = _runner(tmp_path)
    _commits(monkeypatch, "", "2222222")
    assert api_pipeline._runner_drift_refusal(_project(tmp_path, runner)) is None
