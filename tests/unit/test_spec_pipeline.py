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

PACKAGE = "docs/spec/spec"
AUTHOR_STAGES = (
    "spec-context",
    "spec-requirements",
    "spec-requirements-review",
    "spec-design",
    "spec-tests",
    "spec-readiness",
)


def test_only_the_rollback_target_declares_max_rollbacks() -> None:
    """AUD3 (0288): the budget lives on the rollback TARGET, nowhere else.

    The engine takes the limit from ``stages[target_idx].max_rollbacks``
    (``_rollback_budget_allow``), so a budget on a rollback SOURCE — or on a
    stage no rollback targets — is dead configuration. Both reviews roll back
    to ``spec-requirements``: the budget must sit there and only there.
    """
    from awf.pipeline import _ROLLBACK_TO_KEYS

    stages, _description = setup._variant_composition("spec")
    # QA2 (0289): derive the targets from the SAME key set the resolver
    # reads (``on_blocked`` and ``on_rejected``). Scanning only
    # ``on_rejected`` would miss a future ``on_blocked: rollback_to:X`` and
    # then praise a budget on X as "dead" (or miss one that X lacks).
    targets = {
        str(stage.get(key) or "").split("rollback_to:", 1)[1]
        for stage in stages
        for key in _ROLLBACK_TO_KEYS
        if str(stage.get(key) or "").startswith("rollback_to:")
    }
    assert targets == {"spec-requirements"}, targets
    carrying = {stage["name"] for stage in stages if "max_rollbacks" in stage}
    assert carrying == targets, (
        "max_rollbacks is read only from the rollback TARGET; stages carrying "
        f"a (possibly inert) budget: {sorted(carrying)}"
    )
    target = next(s for s in stages if s["name"] == "spec-requirements")
    assert target["max_rollbacks"] == 3


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
    assert [stage["name"] for stage in stages] == ["plan", *AUTHOR_STAGES, "verify"]
    author = stages[1:-1]
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


# ─── S12 (TODO-0267): slug parameterization, plan stage, auto-FAIL ───────


def _write_spec_pipeline(repo: Path, slug: str = "fixture") -> Path:
    result, _unresolved = setup.write_spec_pipeline(repo, slug)
    return Path(result.file)


def test_template_and_generated_pipeline_have_plan_first(tmp_path: Path) -> None:
    from awf.pipeline import load_stages

    template = setup._builtin_pipeline_template("spec")
    assert template is not None
    template_stages = load_stages(template)
    assert template_stages[0].name == "plan"
    assert template_stages[0].kind == "plan"
    assert template_stages[1].kind == "execute"

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    pipeline = _write_spec_pipeline(repo, "fixture")
    stages = load_stages(pipeline)
    assert stages[0].name == "plan"
    assert stages[0].kind == "plan"
    assert stages[1].kind == "execute"
    text = pipeline.read_text(encoding="utf-8")
    assert "spec-pipeline" not in text
    for stage in stages[1:-1]:
        assert stage.output.startswith("docs/spec/fixture/fixture.")


def test_spec_init_writes_two_slugs_and_refuses_overwrite(tmp_path: Path) -> None:
    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    first = _write_spec_pipeline(repo, "alpha")
    second = _write_spec_pipeline(repo, "carry-cd")

    assert first.name == "spec-alpha.yaml"
    assert second.name == "spec-carry-cd.yaml"
    assert first.is_file() and second.is_file()
    assert "docs/spec/alpha/alpha.md" in first.read_text(encoding="utf-8")
    assert "docs/spec/carry-cd/carry-cd.md" in second.read_text(encoding="utf-8")
    assert "spec-pipeline" not in first.read_text(encoding="utf-8")

    with pytest.raises(api.AwfApiError):
        setup.write_spec_pipeline(repo, "alpha")
    result, _unresolved = setup.write_spec_pipeline(repo, "alpha", force=True)
    assert result.overwritten

    with pytest.raises(api.AwfApiError):
        setup.write_spec_pipeline(repo, "../escape")


def test_fail_review_rolls_back_without_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from awf import pipeline_engine
    from awf.pipeline import load_stages
    from awf.transitions import resolve_transition

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    stages = load_stages(_write_spec_pipeline(repo, "fixture"))
    review = next(s for s in stages if s.name == "spec-requirements-review")
    review_idx = stages.index(review)
    target = repo / review.output
    target.parent.mkdir(parents=True)
    target.write_text("# Ревью\n\nСтатус: FAIL\n", encoding="utf-8")

    assert pipeline_engine._declared_review_verdict(repo, review.output) == "rejected"
    action, rollback_target = resolve_transition(review, "rejected")
    assert (action, rollback_target) == ("rollback", "spec-requirements")

    def _no_supervisor(*_args, **_kwargs):
        raise AssertionError("the automatic FAIL must not call the supervisor")

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", _no_supervisor)
    new_idx, new_todo, rc = pipeline_engine._handle_direct_rollback(
        repo,
        repo / ".agentic" / "logs",
        stages,
        "TODO-0001",
        rollback_target,
        source=review.name,
    )
    assert rc == 0
    assert new_todo == "TODO-0001"
    assert stages[new_idx].name == "spec-requirements"
    assert new_idx < review_idx


def test_pass_review_advances_to_design(tmp_path: Path) -> None:
    from awf import pipeline_engine
    from awf.pipeline import load_stages
    from awf.transitions import resolve_transition

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    stages = load_stages(_write_spec_pipeline(repo, "fixture"))
    review = next(s for s in stages if s.name == "spec-requirements-review")
    target = repo / review.output
    target.parent.mkdir(parents=True)
    target.write_text("# Ревью\n\nСтатус: PASS\n", encoding="utf-8")

    assert pipeline_engine._declared_review_verdict(repo, review.output) == ""
    action, _target = resolve_transition(review, "done")
    assert action == "next"
    assert stages[stages.index(review) + 1].name == "spec-design"


def test_declared_fail_rewinds_through_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage wiring, not just the helper: DONE + ``Статус: FAIL`` → the
    requirements stage, and no supervisor stage is entered."""
    from awf import paths, pipeline_engine
    from awf.pipeline import load_stages

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    stages = load_stages(_write_spec_pipeline(repo, "fixture"))
    review_idx = next(
        i for i, s in enumerate(stages) if s.name == "spec-requirements-review"
    )
    review = stages[review_idx]
    todo = "TODO-0001"
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo}.md").write_text("# Task\n", encoding="utf-8")

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **_kw):
        output = project_dir / stage.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("# Ревью\n\nСтатус: FAIL\n", encoding="utf-8")
        outbox = paths.outbox(project_dir)
        (outbox / f"DONE-{todo_id}.md").write_text("FAIL\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    def _no_supervisor(*_args, **_kwargs):
        raise AssertionError("an automatic FAIL must not enter a supervisor stage")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", _no_supervisor)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    new_todo, new_idx, rc = pipeline_engine.execute_agent_stage(
        review,
        todo,
        repo,
        {},
        paths.logs_dir(repo),
        stages,
        review_idx,
        [0] * len(stages),
        auto=False,
        agent_hard_timeout=None,
    )

    assert rc == 0
    assert new_todo == todo
    assert stages[new_idx].name == "spec-requirements"
    assert new_idx < review_idx

