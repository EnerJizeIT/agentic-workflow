"""A48: ``models.worker.model`` is the project-wide fallback for a role.

A role with no own model inherits the worker model instead of leaving
``--model`` out (which used to select opencode's default, ``vllm/llm``).
The refusal itself lives in ``test_role_model_required.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.agent_stage import run_agent_stage
from awf.pipeline import Stage
from awf.supervisor import run_supervisor_stage
from tests.unit._role_model_fixtures import WORKER_MODEL, FakePopen, patch_spawn


def test_agent_spawn_uses_worker_model_and_passes_model_flag(
    role_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = role_project({
        "models": {
            "supervisor": {"description": "no own model"},
            "worker": {"model": WORKER_MODEL},
        }
    })
    patch_spawn(monkeypatch)

    stage = Stage(name="execute", role="supervisor", kind="execute")
    run_agent_stage(
        stage, "TODO-0001", project_dir,
        yaml.safe_load((project_dir / ".agentic" / "config.yaml").read_text()),
        project_dir / ".agentic" / "logs",
    )

    cmd = FakePopen._last_cmds[-1]
    assert cmd[cmd.index("--model") + 1] == WORKER_MODEL


def test_supervisor_spawn_uses_worker_model(
    role_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = role_project({
        "models": {
            "supervisor": {"description": "no own model"},
            "worker": {"model": WORKER_MODEL},
        }
    })
    patch_spawn(monkeypatch)

    stage = Stage(name="plan", role="supervisor", kind="plan")
    run_supervisor_stage(
        stage, todo_id="", auto=True, project_dir=project_dir,
        logs_dir=project_dir / ".agentic" / "logs",
    )

    cmd = FakePopen._last_cmds[-1]
    assert cmd[cmd.index("--model") + 1] == WORKER_MODEL


def test_model_check_reports_inherited_worker_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role with no own model inherits ``models.worker.model`` — the same
    value the spawn will pass, and not a missing one."""
    from awf.api.model_check import check_model_config
    from tests.unit._role_model_fixtures import check_project, hermetic_probe

    hermetic_probe(monkeypatch)
    result = check_model_config(check_project(tmp_path, {
        "supervisor": {"description": "no own model"},
        "worker": {"model": WORKER_MODEL},
    }))

    assert result["missing_model_roles"] == []
    entry = [m for m in result["models"] if m["role"] == "supervisor"][0]
    assert entry["model"] == WORKER_MODEL
    assert entry["valid"] is True
