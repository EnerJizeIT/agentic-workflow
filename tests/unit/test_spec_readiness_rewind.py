"""S14 (TODO-0288): readiness BLOCKED rewinds to the author.

A readiness report that says ``Статус: BLOCKED`` used to end as a normal
DONE: the stage advanced to verify and a human had to kill the run. Now the
report is a verdict, like a review ``FAIL``: the same unit returns to the
author named by the first open question — requirements or architecture —
without a supervisor replan, and never to verify.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import paths, pipeline_engine
from awf.api import setup
from awf.pipeline import load_stages
from tests.spec_support import init_project_repo

SLUG = "fixture"


def _write_roles(repo: Path) -> None:
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    for role in setup._stage_roles(setup._variant_composition("spec")[0]):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")


def _stages(repo: Path):
    _write_roles(repo)
    result, _unresolved = setup.write_spec_pipeline(repo, SLUG)
    return load_stages(Path(result.file))


def _readiness_text(owner: str = "аналитик") -> str:
    return (
        "# Готовность\n\n"
        "Статус: BLOCKED\n"
        "Ревизия: ready-2\n"
        "Входы: fixture.md@req-1\n"
        "База кода: commit `abcdef1234`\n"
        "Проверено: fixture.md=req-1\n"
        "Пакет-sha: " + "a" * 64 + "\n\n"
        "## Открытые вопросы\n\n"
        "| ID | Вопрос | Владелец | Влияние |\n"
        "|---|---|---|---|\n"
        f"| Q1 | Пробел в документе | {owner} | блокирует R1 |\n"
    )


def _readiness_stage(repo: Path):
    stages = _stages(repo)
    index = next(
        i for i, stage in enumerate(stages) if stage.name == "spec-readiness"
    )
    return stages, index


def test_blocked_readiness_is_a_rejected_verdict(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    stage = stages[index]
    path = repo / stage.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_readiness_text(), encoding="utf-8")

    assert pipeline_engine._declared_review_verdict(repo, stage.output) == "rejected"


def test_ready_readiness_keeps_the_normal_advance(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    stage = stages[index]
    path = repo / stage.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _readiness_text().replace("Статус: BLOCKED", "Статус: READY"),
        encoding="utf-8",
    )

    assert pipeline_engine._declared_review_verdict(repo, stage.output) == ""


def test_requirements_owner_rewinds_to_the_analyst_stage(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    stage = stages[index]
    path = repo / stage.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_readiness_text("аналитик"), encoding="utf-8")

    target = pipeline_engine._readiness_rollback_target(
        repo, stage.output, stages
    )
    assert target == "spec-requirements"


def test_architecture_owner_rewinds_to_the_architect_stage(tmp_path: Path):
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    stage = stages[index]
    path = repo / stage.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_readiness_text("архитектор"), encoding="utf-8")

    target = pipeline_engine._readiness_rollback_target(
        repo, stage.output, stages
    )
    assert target == "spec-design"


def test_engine_rewinds_blocked_readiness_to_the_author_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    stage = stages[index]
    todo = "TODO-0001"
    inbox = repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo}.md").write_text("# Task\n", encoding="utf-8")

    def mock_agent(stage_obj, todo_id, project_dir, config, logs_dir, **_kw):
        target = project_dir / stage_obj.output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_readiness_text("аналитик"), encoding="utf-8")
        outbox = paths.outbox(project_dir)
        (outbox / f"DONE-{todo_id}.md").write_text(
            "readiness BLOCKED\n", encoding="utf-8"
        )
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(
        pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None
    )
    monkeypatch.setattr(
        pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None
    )
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    new_todo, new_idx, rc = pipeline_engine.execute_agent_stage(
        stage,
        todo,
        repo,
        {},
        paths.logs_dir(repo),
        stages,
        index,
        [0] * len(stages),
        auto=False,
        agent_hard_timeout=None,
    )

    assert rc == 0
    assert new_todo == todo
    assert stages[new_idx].name != "verify", (
        "BLOCKED readiness must go back to an author, not forward to verify"
    )
    assert stages[new_idx].name in ("spec-requirements", "spec-design")


def _write_readiness(repo: Path, stage, text: str) -> str:
    path = repo / stage.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return stage.output


def test_blocked_readiness_without_open_questions_rewinds_to_the_root_author(
    tmp_path: Path,
):
    """No owner table: the package's root author (analyst) is the fallback."""
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    header = _readiness_text().split("## Открытые вопросы")[0]
    output = _write_readiness(repo, stages[index], header)

    assert pipeline_engine._declared_review_verdict(repo, output) == "rejected"
    assert (
        pipeline_engine._readiness_rollback_target(repo, output, stages)
        == "spec-requirements"
    )


def test_english_architecture_owner_rewinds_to_the_architect_stage(tmp_path: Path):
    """A report written in English must not fall back to the analyst."""
    repo = init_project_repo(tmp_path)
    stages, index = _readiness_stage(repo)
    output = _write_readiness(repo, stages[index], _readiness_text("architecture"))

    assert (
        pipeline_engine._readiness_rollback_target(repo, output, stages)
        == "spec-design"
    )


def test_blocked_status_in_an_author_document_is_not_a_readiness_verdict(
    tmp_path: Path,
):
    """Only the readiness file is a readiness verdict (shared reader guard)."""
    repo = init_project_repo(tmp_path)
    stages, _index = _readiness_stage(repo)
    design = next(stage for stage in stages if stage.name == "spec-design")
    path = repo / design.output
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Архитектура\n\nСтатус: BLOCKED\n", encoding="utf-8")

    assert pipeline_engine._declared_review_verdict(repo, design.output) == ""
    assert pipeline_engine._readiness_rollback_target(repo, design.output, stages) == ""
