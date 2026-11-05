"""A32: a review refusal inside a live unit does not grow the queue.

The 01.11 spec run looped four times: a reviewer returned FAIL, the engine
rolled back, spawned ``opencode run --title awf-supervisor-replan`` with the
whole handoff history, the subprocess died on HTTP 400, and the run was over.
Along the way the replan grew clone units (TODO-0327…0332) that had to be
retired one by one.

This file pins the refusal itself: the report goes into the SAME TODO, the
stage rewinds, the inbox stays as it was, and the dispatcher never reaches
the replan unless the policy says ``replan``. The replan path (bounded
context, crash resilience, retired ids) is pinned next door in
``test_reject_replan_guard.py``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import paths, pipeline_engine, rejection
from awf.pipeline import Stage


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "handoff"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: A32\n")
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "---\nverify: [\"true\"]\n---\n# TODO — x\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / "TODO-0001.ready").write_text("", encoding="utf-8")
    (project / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text(
        "## Task 1 · [x]\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    return project


def _stages(on_rejected: str = "escalate") -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="implement", role="agent-implementer", kind="execute"),
        Stage(
            name="review",
            role="agent-architect-review",
            kind="execute",
            on_rejected=on_rejected,
        ),
        Stage(name="verify", role="supervisor", kind="verify", on_rejected="replan"),
    ]


def _review(project: Path, text: str = "CTX1: the empty name breaks the log") -> None:
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        f"# REVIEW\n\n- {text}\n", encoding="utf-8"
    )


def _inbox_todos(project: Path) -> list[str]:
    return sorted(p.name for p in paths.inbox(project).glob("TODO-*.md"))


def test_two_rejections_make_two_rounds_in_the_same_unit(tmp_path):
    """Acceptance: two FAILs in a row, one unit, no new TODO files."""
    project = _project(tmp_path)
    _review(project)
    before = _inbox_todos(project)

    outcomes = [
        pipeline_engine._handle_rejected(
            project,
            paths.logs_dir(project),
            _stages(),
            "TODO-0001",
            _stages()[2],
            2,
        )
        for _ in range(2)
    ]

    assert [(idx, todo, rc) for idx, todo, rc in outcomes] == [
        (1, "TODO-0001", 0),
        (1, "TODO-0001", 0),
    ]
    assert _inbox_todos(project) == before
    todo_text = (paths.inbox(project) / "TODO-0001.md").read_text(encoding="utf-8")
    assert todo_text.count("## REVIEW-фикс") == 2


def test_a_rollback_to_rejection_also_rewinds_the_same_unit(tmp_path):
    project = _project(tmp_path)
    _review(project)
    before = _inbox_todos(project)

    idx, todo, rc = pipeline_engine._handle_rejected(
        project,
        paths.logs_dir(project),
        _stages("rollback_to:implement"),
        "TODO-0001",
        _stages()[2],
        2,
        target="implement",
    )

    assert (idx, todo, rc) == (1, "TODO-0001", 0)
    assert _inbox_todos(project) == before
    assert "## REVIEW-фикс" in (paths.inbox(project) / "TODO-0001.md").read_text(
        encoding="utf-8"
    )


def test_only_an_explicit_replan_may_create_a_unit():
    """Acceptance: replan is the ONE policy value that grows the queue."""
    for policy in ("escalate", "rollback_to:implement", "fix_round:implement"):
        assert rejection.rejection_stays_in_unit(_stages(policy)[2]), policy
    assert not rejection.rejection_stays_in_unit(_stages("replan")[2])


def test_a_rejection_without_an_earlier_author_stops_without_a_unit(tmp_path):
    """No author to rewind to — stop with a reason, never escalate to replan.

    An escalate here would spawn the supervisor subprocess under a policy
    that did not sanction a new unit (the 01.11 clone factory).
    """
    project = _project(tmp_path)
    stages = [
        Stage(name="review", role="agent-architect-review", kind="execute"),
    ]
    before = _inbox_todos(project)

    outcome = pipeline_engine._handle_rejected(
        project,
        paths.logs_dir(project),
        stages,
        "TODO-0001",
        stages[0],
        0,
    )

    assert outcome == (-1, "TODO-0001", 1)
    assert _inbox_todos(project) == before
    log = (paths.logs_dir(project) / "orchestrator.log").read_text(encoding="utf-8")
    assert "no earlier author stage" in log


# ─── dispatcher wiring (A32) ────────────────────────────────────────────
# The architect-review asked QA to cover what the handler tests skip: the
# two dispatcher branches (``escalate`` and ``rollback``) that decide
# whether a refusal rewinds. These tests go through ``execute_agent_stage``.


def _wire_engine(monkeypatch, supervisor):
    """A worker that leaves a rejection, and a supervisor that must not run."""

    def worker(_stage, todo_id, project_dir, _config, _logs_dir, **_kw):
        for name in ("REVIEW-{todo}.md", "REVIEW-REJECTED-{todo}.ready"):
            target = paths.outbox(project_dir) / name.format(todo=todo_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# review\n\nCTX1: the finding\n", encoding="utf-8")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", supervisor)
    monkeypatch.setattr(
        pipeline_engine, "_ensure_baseline_sha", lambda *_a, **_k: None
    )
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *_a, **_k: None)


def _run_review_stage(project, stages):
    return pipeline_engine.execute_agent_stage(
        stage=stages[2],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=paths.logs_dir(project),
        stages=stages,
        stage_idx=2,
        retry_counts=[0] * len(stages),
        auto=True,
        agent_hard_timeout=None,
    )


def test_escalate_dispatcher_rewinds_instead_of_replanning(tmp_path, monkeypatch):
    """A rejection with ``on_rejected: escalate`` must not reach the replan.

    The handler test calls ``_handle_rejected``; this one proves the
    dispatcher asks it first, so a regression in the branch order (the
    actual 01.11 failure: replan after every review) cannot come back.
    """
    project = _project(tmp_path)
    calls: list[str] = []
    _wire_engine(monkeypatch, lambda *_a, **_k: calls.append("replan") or "")

    todo, idx, rc = _run_review_stage(project, _stages())

    assert (todo, idx, rc) == ("TODO-0001", 1, 0)
    assert calls == [], "a rejection must not spawn the replan subprocess"
    assert _inbox_todos(project) == ["TODO-0001.md"]
    assert "## REVIEW-фикс" in (
        paths.inbox(project) / "TODO-0001.md"
    ).read_text(encoding="utf-8")


def test_rollback_dispatcher_rewinds_instead_of_replanning(tmp_path, monkeypatch):
    """``on_rejected: rollback_to:`` takes the rollback branch — same rule."""
    project = _project(tmp_path)
    calls: list[str] = []
    _wire_engine(monkeypatch, lambda *_a, **_k: calls.append("replan") or "")
    stages = _stages("rollback_to:implement")

    todo, idx, rc = _run_review_stage(project, stages)

    assert (todo, idx, rc) == ("TODO-0001", 1, 0)
    assert calls == []
    assert _inbox_todos(project) == ["TODO-0001.md"]


def test_a_rejection_with_a_missing_rollback_target_still_rewinds(
    tmp_path, monkeypatch
):
    """A32: a bad ``rollback_to:`` must not hand the refusal to a replan.

    The declared target is missing (typo, an old pipeline), so the unit
    rewinds to the nearest earlier author instead of escalating into a
    path that can grow the queue.
    """
    project = _project(tmp_path)
    calls: list[str] = []
    _wire_engine(monkeypatch, lambda *_a, **_k: calls.append("replan") or "")
    stages = _stages("rollback_to:gone")

    todo, idx, rc = _run_review_stage(project, stages)

    assert (todo, idx, rc) == ("TODO-0001", 1, 0)
    assert calls == []
    assert _inbox_todos(project) == ["TODO-0001.md"]


def test_a_rejection_on_the_first_stage_stops_instead_of_creating_a_unit(
    tmp_path, monkeypatch
):
    """B3 (architect-review): no author, no replan, no new unit.

    The escalate branch used to hand a rejection with no earlier author to
    ``_handle_escalate`` — the clone factory of 01.11. A policy that does
    not say ``replan`` has nothing to replan here, so the run stops with a
    reason and the inbox stays as it was.
    """
    project = _project(tmp_path)
    calls: list[str] = []
    _wire_engine(monkeypatch, lambda *_a, **_k: calls.append("replan") or "")
    stages = [
        Stage(
            name="review",
            role="agent-architect-review",
            kind="execute",
            on_rejected="escalate",
        ),
    ]

    todo, idx, rc = pipeline_engine.execute_agent_stage(
        stage=stages[0],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=paths.logs_dir(project),
        stages=stages,
        stage_idx=0,
        retry_counts=[0],
        auto=True,
        agent_hard_timeout=None,
    )

    assert (todo, rc) == ("TODO-0001", 1)
    assert calls == []
    assert _inbox_todos(project) == ["TODO-0001.md"]
    log = (paths.logs_dir(project) / "orchestrator.log").read_text(encoding="utf-8")
    assert "no earlier author stage" in log


def test_replan_policy_still_spawns_the_supervisor(tmp_path, monkeypatch):
    """The one sanctioned case is not swallowed by the rewind branch."""
    project = _project(tmp_path)
    (paths.inbox(project) / "TODO-0002.md").write_text(
        "---\nreplan_for: TODO-0001\n---\n# refined\n", encoding="utf-8"
    )
    (paths.inbox(project) / "TODO-0002.ready").write_text("", encoding="utf-8")
    calls: list[str] = []
    _wire_engine(
        monkeypatch, lambda *_a, **_k: calls.append("replan") or "TODO-0002"
    )
    stages = _stages("replan")

    todo, idx, rc = _run_review_stage(project, stages)

    assert (todo, idx, rc) == ("TODO-0002", 0, 0)
    assert calls == ["replan"]
