"""A48: ``awf doctor`` names every role's model and warns before launch.

The rows read ``check_model_config``, whose checked set is the ``models:``
keys ∪ the pipeline stage roles. A role the spawn would refuse must be a
WARN in doctor, not a failure at the first stage.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.doctor import run_doctor
from tests.unit._role_model_fixtures import (
    WORKER_MODEL,
    check_project,
    doctor_project,
    fake_model_check,
    hermetic_probe,
)


def test_doctor_prints_role_model_line(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config",
        fake_model_check({
            "models": [
                {"role": "worker", "model": WORKER_MODEL, "valid": True},
            ],
            "warnings": [],
            "providers_available": ["opencode-go"],
        }),
    )
    report = run_doctor(doctor_project(tmp_path))
    assert any(
        line.startswith("ok  models:") and f"роль worker: модель {WORKER_MODEL}" in line
        for line in report.lines
    ), "\n".join(report.lines)


def test_doctor_warns_for_role_without_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config",
        fake_model_check({
            "models": [
                {"role": "supervisor", "model": "(none)", "valid": False},
            ],
            "warnings": [
                "Role 'supervisor': no model — add models.supervisor.model",
            ],
            "missing_model_roles": ["supervisor"],
            "missing_model_warnings": [
                "Role 'supervisor': no model — add models.supervisor.model",
            ],
            "providers_available": ["opencode-go"],
        }),
    )
    report = run_doctor(doctor_project(tmp_path))
    assert any(
        line.startswith("WARN  models:")
        and "у роли supervisor нет модели" in line
        for line in report.warnings
    ), "\n".join(report.lines)
    assert report.rc == 1


def test_doctor_warns_on_cold_start_for_role_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A48/architect F3: a cold opencode answers "no models" and used to
    return before the role loop — the config-level WARN disappeared with it."""
    from awf import doctor

    monkeypatch.setattr(doctor, "_opencode_json_problem", lambda: "")
    monkeypatch.setattr(
        "awf.api.model_check.check_model_config",
        fake_model_check({
            "models": [
                {"role": "supervisor", "model": "(none)", "valid": False},
            ],
            "warnings": [
                "Role 'supervisor': no model — add models.supervisor.model",
            ],
            "providers_available": [],
            "cli_models_available": False,
            "missing_model_roles": ["supervisor"],
        }),
    )

    out = doctor._Lines()
    doctor._check_models(tmp_path, "", out)

    assert any(
        line.startswith("WARN  models:")
        and "у роли supervisor нет модели" in line
        for line in out.warnings
    ), "\n".join(out.lines)


def test_model_check_names_roles_without_any_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine refuses these roles, so doctor must get the list
    structurally — not by parsing the warning text (architect F2)."""
    from awf.api.model_check import check_model_config

    hermetic_probe(monkeypatch)
    result = check_model_config(check_project(
        tmp_path, {"supervisor": {"description": "no model"}}
    ))

    assert result["missing_model_roles"] == ["supervisor"]
    entry = [m for m in result["models"] if m["role"] == "supervisor"][0]
    assert entry["model"] == "(none)"
    assert entry["valid"] is False
    assert any("supervisor" in w for w in result["warnings"])


def test_pipeline_role_absent_from_models_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A48/REVIEW: a role named only by a pipeline stage is checked too.

    ``check_model_config`` used to walk ``models:`` keys only. A stage role
    missing from the config passed that check silently while
    ``require_role_model`` refused it at spawn — doctor must warn BEFORE
    the launch, not at the failure.
    """
    from awf import doctor
    from awf.api import model_check

    project = tmp_path / "pipeline-role"
    agentic = project / ".agentic"
    (agentic / "pipelines").mkdir(parents=True)
    (agentic / "config.yaml").write_text(
        yaml.safe_dump({"models": {}}),
        encoding="utf-8",
    )
    (agentic / "pipelines" / "default.yaml").write_text(
        "name: default\n"
        "stages:\n"
        "  - name: plan\n"
        "    role: supervisor\n"
        "  - name: qa\n"
        "    role: agent-qa-review\n",
        encoding="utf-8",
    )
    # Hermetic model discovery: no CLI, no opencode.json, no history.
    monkeypatch.setattr(model_check, "_load_cli_models", lambda: set())
    monkeypatch.setattr(
        model_check, "_load_recent_models", lambda *a, **kw: set()
    )
    monkeypatch.setattr(
        model_check, "_load_opencode_config", lambda path: ({}, {})
    )
    monkeypatch.setattr(doctor, "_opencode_json_problem", lambda: "")

    result = model_check.check_model_config(project)
    assert "agent-qa-review" in result["missing_model_roles"], result

    out = doctor._Lines()
    doctor._check_models(project, "", out)
    assert any(
        line.startswith("WARN  models:")
        and "agent-qa-review" in line
        for line in out.warnings
    ), "\n".join(out.lines)
