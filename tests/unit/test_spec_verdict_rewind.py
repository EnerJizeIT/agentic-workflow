"""S12 (TODO-0267) QA: the review verdict is real, bounded and narrow.

The requirements-review document decides the stage: a FRESH ``Статус: FAIL``
rewinds it to the author without a supervisor replan, a stale leftover does
not, the rewind is budgeted, and only the review document carries a verdict.

Kept beside (not inside) ``test_spec_pipeline.py``: that file holds the
composition/generation contract; this one holds the engine behaviour.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from awf.api import setup
from tests.spec_support import init_project_repo


def _write_roles(repo: Path) -> None:
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    for role in setup._stage_roles(setup._variant_composition("spec")[0]):
        (roles / f"{role}.md").write_text(f"# {role}\n", encoding="utf-8")


def _write_spec_pipeline(repo: Path, slug: str = "fixture") -> Path:
    result, _unresolved = setup.write_spec_pipeline(repo, slug)
    return Path(result.file)


def test_generated_pipeline_carries_no_template_token(tmp_path: Path) -> None:
    """The token is the template's ONLY spelling of the package path.

    A leftover ``{{spec_slug}}`` would create a literal directory named
    ``{{spec_slug}}`` at runtime — the package would never be found.
    """
    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    pipeline = _write_spec_pipeline(repo, "fixture")
    assert "{{spec_slug}}" not in pipeline.read_text(encoding="utf-8")


def test_stale_fail_report_is_not_the_stages_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover FAIL from an earlier attempt must not rewind the stage.

    The declared-output freshness check runs BEFORE the verdict read: a
    document that predates this stage's start is the M3.2 blocked case,
    not the reviewer's decision. Without this order, a stale FAIL would
    rewind forever while the fresh stage never wrote anything.
    """
    from awf import paths, pipeline_engine
    from awf.pipeline import load_stages
    from awf.pipeline_state import read_state

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

    leftover = repo / review.output
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("# Ревью\n\nСтатус: FAIL\n", encoding="utf-8")
    old = time.time() - 120
    os.utime(leftover, (old, old))
    # The helper alone still reads FAIL — the ENGINE order is what saves us.
    assert pipeline_engine._declared_review_verdict(repo, review.output) == "rejected"

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **_kw):
        outbox = paths.outbox(project_dir)
        (outbox / f"DONE-{todo_id}.md").write_text(
            "no fresh report\n", encoding="utf-8"
        )
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    def _no_direct_rollback(*_args, **_kwargs):
        raise AssertionError("a stale FAIL must not reach the direct-rollback path")

    calls = {"escalate": 0}

    def fake_escalate(*args, **_kwargs):
        # (project_dir, logs_dir, s_name, current_todo, auto, stage,
        #  retry_counts, stage_idx, pipeline_name)
        calls["escalate"] += 1
        return args[7], args[3], 0

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_handle_direct_rollback", _no_direct_rollback)
    monkeypatch.setattr(pipeline_engine, "_handle_escalate", fake_escalate)
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
    assert calls["escalate"] == 1, "a stale FAIL must route through the stage policy"
    assert new_todo == todo
    assert new_idx == review_idx
    assert "rollback_counts" not in (read_state(repo) or {})


def test_direct_rollback_stops_at_the_target_stage_budget(tmp_path: Path) -> None:
    """The automatic rewind is budgeted: an unchanged FAIL cannot loop.

    Limit = the TARGET stage's ``max_rollbacks`` (3 by default), the same
    rule the supervisor rollback uses.
    """
    from awf import pipeline_engine
    from awf.pipeline import load_stages

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    stages = load_stages(_write_spec_pipeline(repo, "fixture"))
    logs = repo / ".agentic" / "logs"

    outcomes = [
        pipeline_engine._handle_direct_rollback(
            repo, logs, stages, "TODO-0001", "spec-requirements",
            source="spec-requirements-review",
        )
        for _ in range(4)
    ]

    assert [rc for _idx, _todo, rc in outcomes] == [0, 0, 0, 1]
    for idx, todo, rc in outcomes[:3]:
        assert todo == "TODO-0001"
        assert stages[idx].name == "spec-requirements"


def test_only_the_review_document_carries_a_verdict(tmp_path: Path) -> None:
    """A status line quoted in ANOTHER package document is not a verdict."""
    from awf import pipeline_engine
    from awf.pipeline import load_stages

    repo = init_project_repo(tmp_path)
    _write_roles(repo)
    stages = load_stages(_write_spec_pipeline(repo, "fixture"))
    requirements = next(s for s in stages if s.name == "spec-requirements")
    target = repo / requirements.output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Требования\n\nСтатус: FAIL\n", encoding="utf-8")

    assert pipeline_engine._declared_review_verdict(repo, requirements.output) == ""
