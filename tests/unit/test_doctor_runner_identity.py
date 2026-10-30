"""R5 (ops reliability): what the pinned runner row says about its code.

A version string is not identity (AUD-0293-2); the doctor adds the commit,
then the working-tree state (AUD-0296d-1), names "no commit evidence" for a
checkout outside git (d-2), and tells three unreadable-checkout breakages
apart (d-3). Split from ``test_doctor_audit_fixes.py`` to keep both files
under the ratchet's 400-line bar; the fixtures mirror that file.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from awf.doctor import run_doctor

CLEAN_CONFIG = {
    "project": {"name": "Test"},
    "config_version": 2,
    "discipline": {"dirty_policy": "isolate"},
    "verification": {"test_cmd": "pytest -q", "lint_cmd": "ruff check ."},
    "automation": {"stage_timeout_seconds": 3600},
    "models": {"worker": {"model": "p/one", "agent_name": "worker"}},
}

FAKE_MODELS = {
    "models": [{"role": "worker", "model": "p/one", "provider": "p", "valid": True}],
    "warnings": [],
    "providers_available": ["p"],
    "role_files": [],
}


def _write_config(root, config) -> None:
    (root / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".agentic").mkdir(parents=True)
    _write_config(root, CLEAN_CONFIG)
    return root


@pytest.fixture
def env(monkeypatch, tmp_path):
    opencode = tmp_path / "opencode.json"
    opencode.write_text(json.dumps({"provider": {}}), encoding="utf-8")
    monkeypatch.setattr("awf.doctor.xdg.opencode_config_file", lambda: opencode)
    monkeypatch.setattr("awf.versions.process_commit", lambda: "proc0000")
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config", lambda project_dir: dict(FAKE_MODELS)
    )
    return opencode


def _versions(monkeypatch, *, process="2.2.0", plugin="2.2.0", metadata="2.2.0"):
    monkeypatch.setattr("awf.versions.process_version", lambda: process)
    monkeypatch.setattr("awf.versions.plugin_version", lambda: plugin)
    monkeypatch.setattr("awf.versions.install_metadata", lambda name: metadata)


def _lines(report) -> str:
    return "\n".join(report.lines)


def _git_repo(path: Path) -> None:
    """A real one-commit repo at *path* (the dirty count is a real git read)."""
    path.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                *args,
            ],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-q")
    git("add", ".")
    git("commit", "-q", "-m", "base")


def _pinned_runner(
    project, monkeypatch, *, git: bool = False, stub_commit: bool = True
) -> Path:
    """A runner checkout pinned by the project config; optionally a git repo."""
    runner = project.parent / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text('__version__ = "2.2.0"\n')
    if git:
        _git_repo(runner)
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)
    if stub_commit:
        # env pins process_commit to "proc0000"; the pinned runner matches it.
        monkeypatch.setattr("awf.versions.repo_commit", lambda path: "proc0000")
    return runner


def test_same_head_dirty_runner_is_a_warn(project, env, monkeypatch):
    """AUD-0296d-1: same HEAD, 25 uncommitted files — the engine runs them."""
    runner = _pinned_runner(project, monkeypatch, git=True)
    for i in range(25):
        (runner / f"wip{i:02d}.py").write_text("# work in progress\n")

    report = run_doctor(project)
    warns = [line for line in report.warnings if line.startswith("WARN  runner:")]
    assert len(warns) == 1, _lines(report)
    assert "25 changed file(s)" in warns[0], warns[0]
    assert "dirty" in warns[0], warns[0]
    assert report.rc == 1


def test_clean_runner_at_the_process_commit_is_ok(project, env, monkeypatch):
    """AUD-0296d-1: the same repo with no edits stays a healthy runner row."""
    _pinned_runner(project, monkeypatch, git=True)

    report = run_doctor(project)
    assert report.rc == 0, _lines(report)
    assert any(line.startswith("ok  runner:") for line in report.lines), _lines(report)


def test_runner_without_git_says_no_commit_evidence(project, env, monkeypatch):
    """AUD-0296d-2: no ``.git`` → ok with the evidence named, not a bare ok."""
    _pinned_runner(project, monkeypatch, stub_commit=False)

    report = run_doctor(project)
    assert report.rc == 0, _lines(report)
    runner_lines = [
        line for line in report.lines if line.startswith("ok  runner:")
    ]
    assert len(runner_lines) == 1, _lines(report)
    assert "no commit evidence" in runner_lines[0], runner_lines[0]


@pytest.mark.parametrize(
    ("kind", "needle"),
    [
        ("missing", "missing dir"),
        ("no_init", "no awf/__init__.py"),
        ("no_version", "no __version__"),
    ],
)
def test_runner_breakages_get_distinct_texts(
    project, env, monkeypatch, kind, needle
):
    """AUD-0296d-3: three breakages, three facts — not one bare ``unknown``."""
    runner = project.parent / "runner"
    if kind != "missing":
        (runner / "awf").mkdir(parents=True)
    if kind == "no_version":
        (runner / "awf" / "__init__.py").write_text("# no version here\n")
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)

    report = run_doctor(project)
    warns = [line for line in report.warnings if line.startswith("WARN  runner:")]
    assert len(warns) == 1, _lines(report)
    assert needle in warns[0], warns[0]
    assert "unknown" in warns[0], warns[0]
    assert report.rc == 1


def test_commitless_dirty_runner_is_still_a_warn(project, env, monkeypatch):
    """AUD-0296d-1: a git repo with no commits still has a working tree.

    ``repo_commit`` is empty here, but ``git status`` is readable and the
    engine would import the untracked files: "no commit evidence" must not
    swallow the dirt (only a checkout outside git has no evidence at all).
    """
    runner = project.parent / "runner"
    (runner / "awf").mkdir(parents=True)
    (runner / "awf" / "__init__.py").write_text('__version__ = "2.2.0"\n')
    subprocess.run(["git", "init", "-q"], cwd=runner, check=True)
    (runner / "wip.py").write_text("# work in progress\n")
    config = dict(CLEAN_CONFIG)
    config["automation"] = {"runner_dir": str(runner)}
    _write_config(project, config)
    _versions(monkeypatch)
    monkeypatch.setattr("awf.versions.repo_commit", lambda path: "")

    report = run_doctor(project)
    warns = [line for line in report.warnings if line.startswith("WARN  runner:")]
    assert len(warns) == 1, _lines(report)
    assert "no commit evidence" in warns[0], warns[0]
    assert "tree dirty (" in warns[0], warns[0]
    assert "changed file(s)" in warns[0], warns[0]
    assert report.rc == 1
