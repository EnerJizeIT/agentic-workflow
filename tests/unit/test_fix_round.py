"""A7: the review round runs itself — findings appended, stage rewound.

The manual loop is "supervisor reads the review, writes ``## REVIEW-фикс``,
restarts the implementer". With ``on_rejected: fix_round:<stage>`` the engine
does the two steps in one transition: the reviewer's own text becomes the
next instruction, the same unit is rewound, and the shared rollback budget
still caps the loop.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import pipeline_engine
from awf.pipeline import Stage
from awf.transitions import resolve_transition


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "handoff"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: A7\n")
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "---\nverify: [\"true\"]\n---\n# TODO — x\n", encoding="utf-8"
    )
    # The unit is STARTED (the append refuses a queued one).
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


def _stages() -> list[Stage]:
    return [
        Stage(name="implement", role="agent-implementer", kind="execute"),
        Stage(
            name="review",
            role="agent-architect-review",
            kind="execute",
            on_rejected="fix_round:implement",
        ),
    ]


def test_the_policy_resolves_to_a_fix_round():
    action, target = resolve_transition(_stages()[1], "rejected")
    assert (action, target) == ("fix_round", "implement")


def test_a_fix_round_target_must_be_an_execute_stage():
    """AUD7: rewinding to a supervisor stage is a load error, not a loop."""
    from awf.api._errors import AwfApiError
    from awf.pipeline import validate_pipeline_stages

    stages = [
        {"name": "plan", "role": "supervisor"},
        {"name": "implement", "role": "agent-implementer"},
        {"name": "review", "role": "agent-architect-review",
         "on_rejected": "fix_round:plan"},
        {"name": "verify", "role": "supervisor"},
    ]

    with pytest.raises(AwfApiError, match="execute"):
        validate_pipeline_stages(stages, "test")


def test_a_fix_round_target_must_exist():
    from awf.api._errors import AwfApiError
    from awf.pipeline import validate_pipeline_stages

    stages = [
        {"name": "plan", "role": "supervisor"},
        {"name": "review", "role": "agent-architect-review",
         "on_rejected": "fix_round:implement"},
        {"name": "verify", "role": "supervisor"},
    ]

    with pytest.raises(AwfApiError, match="not found"):
        validate_pipeline_stages(stages, "test")


def test_the_engine_appends_the_findings_and_rewinds(tmp_path):
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "# REVIEW\n\n- CTX1: the empty name breaks the log\n", encoding="utf-8"
    )

    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        _stages(),
        "TODO-0001",
        "implement",
        source="review",
    )

    assert (idx, todo, rc) == (0, "TODO-0001", 0)
    todo_text = (project / ".agentic" / "inbox" / "TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert "## REVIEW-фикс" in todo_text
    assert "CTX1: the empty name breaks the log" in todo_text


def test_without_findings_the_round_is_refused(tmp_path):
    project = _project(tmp_path)
    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        _stages(),
        "TODO-0001",
        "implement",
        source="review",
    )
    assert rc == 1
    assert "REVIEW-фикс" not in (
        project / ".agentic" / "inbox" / "TODO-0001.md"
    ).read_text(encoding="utf-8")


def test_a_refused_round_does_not_spend_the_rollback_budget(tmp_path):
    """No findings → no round → the budget is not charged.

    Charging it made a reviewer that rejects without a report exhaust the
    shared budget, and the pipeline then stopped with "budget exhausted"
    instead of the real reason.
    """
    project = _project(tmp_path)
    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        _stages(),
        "TODO-0001",
        "implement",
        source="review",
    )

    assert (idx, rc) == (-1, 1)
    state_file = project / ".agentic" / "state" / "current.yaml"
    state = state_file.read_text(encoding="utf-8") if state_file.is_file() else ""
    assert "rollback_counts" not in state or "TODO-0001" not in state


def test_the_budget_caps_the_loop_and_the_refusal_appends_nothing(tmp_path):
    """Three rounds, then a refusal — the loop is bounded (default 3).

    The fourth call must stop BEFORE the append: a section in the TODO with
    no rewind is the half-loop the handler forbids, and a reviewer would
    read the same findings again with no new work behind them.
    """
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "findings\n", encoding="utf-8"
    )

    outcomes = [
        pipeline_engine._handle_fix_round(
            project,
            project / ".agentic" / "logs",
            _stages(),
            "TODO-0001",
            "implement",
            source="review",
        )
        for _ in range(4)
    ]

    assert [rc for _idx, _todo, rc in outcomes] == [0, 0, 0, 1]
    todo_text = (project / ".agentic" / "inbox" / "TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert todo_text.count("## REVIEW-фикс") == 3


def test_a_missing_target_is_refused(tmp_path):
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "findings", encoding="utf-8"
    )
    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        _stages(),
        "TODO-0001",
        "nope",
        source="review",
    )
    assert rc == 1


def test_an_explicit_id_is_what_the_fix_round_targets(tmp_path):
    """AUD7: the validator and the runtime must address a stage the same way.

    An explicit ``id`` becomes the stage name (``load_stages``), and the
    validator keys stages by ``id or name`` — if the two ever drift, a
    pipeline loads clean and then fails mid-run with "target not found".
    """
    from awf.pipeline import load_stages, validate_pipeline_stages

    stages = [
        {"id": "plan", "role": "supervisor"},
        {"id": "implement", "role": "agent-implementer"},
        {"id": "review", "role": "agent-architect-review",
         "on_rejected": "fix_round:implement"},
        {"id": "verify", "role": "supervisor"},
    ]
    validate_pipeline_stages(stages, "test")

    path = tmp_path / "ids.yaml"
    path.write_text(
        "name: ids\nstages:\n"
        "  - id: plan\n    role: supervisor\n"
        "  - id: implement\n    role: agent-implementer\n"
        "  - id: review\n    role: agent-architect-review\n"
        "    on_rejected: fix_round:implement\n"
        "  - id: verify\n    role: supervisor\n",
        encoding="utf-8",
    )

    loaded = load_stages(path)
    assert [stage.name for stage in loaded] == ["plan", "implement", "review", "verify"]
