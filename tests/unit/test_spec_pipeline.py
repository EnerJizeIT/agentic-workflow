"""SPEC IMP7 — the shipped ``spec`` composition and its declared artifacts.

T1: six author stages, each with its own ``output`` inside the package
directory, verify without an output, the requirements review rolls back to
the author, and all five roles resolve in a temporary project. The setup
path installs the variant as a named pipeline without touching a
user-owned ``spec.yaml``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api
from awf.api import setup
from awf.pipeline_engine import _declared_output_problem
from tests.spec_support import init_project_repo

PACKAGE = "docs/spec/spec-pipeline"
AUTHOR_STAGES = (
    "spec-context",
    "spec-requirements",
    "spec-requirements-review",
    "spec-design",
    "spec-tests",
    "spec-readiness",
)


def _stages() -> list[dict]:
    stages, _description = setup._variant_composition("spec")
    return stages


def test_spec_variant_is_registered() -> None:
    assert "spec" in setup.BUILTIN_PIPELINES
    template = setup._builtin_pipeline_template("spec")
    assert template is not None and template.is_file(), (
        "the packaged template awf/templates/pipelines/spec.yaml is missing"
    )


def test_spec_composition_contract() -> None:
    stages = _stages()
    assert [stage["name"] for stage in stages] == [*AUTHOR_STAGES, "verify"]
    author = stages[:-1]
    for stage in author:
        output = stage.get("output") or ""
        assert output.startswith(f"{PACKAGE}/"), (
            f"{stage['name']} declares an output outside the package: {output!r}"
        )
        assert not stage.get("write_allow"), (
            f"{stage['name']} widens its allowlist beyond the declared output"
        )
    verify = stages[-1]
    assert verify["role"] == "supervisor"
    assert not verify.get("output"), "verify must not declare an artifact"
    assert not verify.get("write_allow")


def test_requirements_review_rolls_back_to_the_author() -> None:
    review = next(s for s in _stages() if s["name"] == "spec-requirements-review")
    assert review["on_rejected"] == "rollback_to:spec-requirements"


def _write_roles(repo: Path) -> None:
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    for role in setup._stage_roles(_stages()):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")


def test_five_roles_resolve_in_a_temp_project(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    assert setup._stage_roles(_stages()) == [
        "agent-architector",
        "agent-project-auditor",
        "agent-qa-review",
        "agent-requirements-reviewer",
        "agent-system-analyst",
    ]
    assert setup.variant_unavailable_roles("spec", repo) == []


def test_setup_names_missing_roles_instead_of_writing(
    tmp_path: Path, monkeypatch
) -> None:
    repo = init_project_repo(tmp_path)
    monkeypatch.setattr(
        setup, "_role_resolves", lambda role, _p: role != "agent-requirements-reviewer"
    )
    monkeypatch.setattr(setup, "_builtin_role_template", lambda role: None)
    result = api.apply_project_setup(repo, team=[], flow_variant="spec")
    assert result.pipeline_file is None
    assert any(
        "agent-requirements-reviewer" in warning for warning in result.warnings
    ), result.warnings


def test_missing_declared_output_names_the_artifact(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    stage = next(s for s in _stages() if s["name"] == "spec-context")
    problem = _declared_output_problem(repo, stage["output"], 0.0)
    assert stage["output"] in problem
    target = repo / stage["output"]
    target.parent.mkdir(parents=True)
    target.write_text("# Контекст\n", encoding="utf-8")
    assert _declared_output_problem(repo, stage["output"], 0.0) == ""


def test_setup_does_not_overwrite_a_user_spec_pipeline(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    spec_yaml = repo / ".agentic" / "pipelines" / "spec.yaml"
    spec_yaml.write_text("# user-owned spec pipeline\n", encoding="utf-8")

    result = api.apply_project_setup(repo, team=[], flow_variant="spec")

    assert result.pipeline_file, "the variant did not install the active pipeline"
    # The active name here is the project default, so the write lands in
    # default.yaml: a spec.yaml the runtime would NOT read stays untouched.
    assert Path(result.pipeline_file).name == "default.yaml"
    assert spec_yaml.read_text(encoding="utf-8") == "# user-owned spec pipeline\n"


def test_write_variant_refuses_existing_spec_pipeline(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    setup.write_variant_pipeline(repo, "spec", "spec")
    with pytest.raises(api.AwfApiError):
        setup.write_variant_pipeline(repo, "spec", "spec")
