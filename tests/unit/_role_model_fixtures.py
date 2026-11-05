"""Shared fixtures for the A48 role-model tests.

Not a test module: pytest collects nothing here. Three test files use it —
``test_role_model_required.py`` (the verify-contract file: resolution and
refusal), ``test_role_model_inheritance.py`` (worker fallback) and
``test_role_model_doctor.py`` (doctor + ``check_model_config``).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

WORKER_MODEL = "opencode-go/deepseek-v4.1-flash"


class FakePopen:
    """Minimal Popen stand-in: records the argv, exits at once."""

    pid = 12345
    _last_cmds: list[list[str]] = []
    stdout = ""
    stderr = ""

    def __init__(self, cmd, cwd=None, **kwargs):
        self.cmd = cmd
        self.returncode = 0
        type(self)._last_cmds.append(list(cmd) if isinstance(cmd, list) else [cmd])

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass

    def communicate(self, input=None, timeout=None):
        return (self.stdout, self.stderr)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @classmethod
    def reset(cls):
        cls._last_cmds = []


def fake_run(*args, **kwargs):
    """``git``/helper calls from the spawn path — success, no output."""
    return subprocess.CompletedProcess(args=args[0] if args else [], returncode=0)


def patch_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``opencode`` non-existent but recorded, and the clock instant."""
    FakePopen.reset()
    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", FakePopen)
    monkeypatch.setattr("awf.signal_watch.subprocess.run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "awf.supervisor.wait_for_supervisor_signal", lambda *_a, **_kw: ""
    )


def make_project(tmp_path: Path, config: dict) -> Path:
    """Minimal project with role files, one TODO and config.yaml."""
    project_dir = tmp_path / "proj"
    agentic = project_dir / ".agentic"
    for name in ("roles", "inbox", "outbox", "context", "logs", "phases"):
        (agentic / name).mkdir(parents=True)
    (agentic / "roles" / "worker.md").write_text("# Worker\n", encoding="utf-8")
    (agentic / "roles" / "supervisor.md").write_text(
        "# Supervisor\nplan/verify\n", encoding="utf-8"
    )
    (agentic / "inbox" / "TODO-0001.md").write_text("task", encoding="utf-8")
    (agentic / "phases" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    config = {
        **config,
        "stage_types": {
            "plan": "plan", "verify": "supervisor", "execute": "execute",
            "agent-dev": "execute", "agent-implementer": "execute",
        },
    }
    (agentic / "config.yaml").write_text(
        yaml.safe_dump(config), encoding="utf-8"
    )
    return project_dir


@pytest.fixture
def role_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Project factory for the spawn tests; a hermetic HOME for role files."""
    fake_home = tmp_path / "home"
    (fake_home / ".config" / "awf" / "roles").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    def build(config: dict) -> Path:
        return make_project(tmp_path, config)

    return build


def doctor_project(tmp_path: Path) -> Path:
    """Project for the doctor rows: config names a role without a model."""
    project_dir = tmp_path / "doctor"
    (project_dir / ".agentic").mkdir(parents=True)
    (project_dir / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump({"models": {"supervisor": {"description": "no model"}}}),
        encoding="utf-8",
    )
    return project_dir


def fake_model_check(result: dict):
    """Replace ``check_model_config`` with a fixed result."""
    return lambda project_dir: dict(result)


def check_project(tmp_path: Path, models: dict) -> Path:
    """Project for ``check_model_config``: ``.agentic/config.yaml`` only."""
    project_dir = tmp_path / "checked"
    (project_dir / ".agentic").mkdir(parents=True)
    (project_dir / ".agentic" / "config.yaml").write_text(
        yaml.safe_dump({"models": models}), encoding="utf-8"
    )
    return project_dir


def hermetic_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``opencode models`` spawn, no real opencode.json, no history."""
    monkeypatch.setattr(
        "awf.api.model_check._load_cli_models", lambda: {WORKER_MODEL}
    )
    monkeypatch.setattr(
        "awf.api.model_check._load_opencode_config", lambda _p: ({}, {})
    )
    monkeypatch.setattr(
        "awf.api.model_check._load_recent_models", lambda _p, _l=None: set()
    )
