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


def test_the_engine_appends_the_findings_and_rewinds(tmp_path):
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "# REVIEW\n\n- CTX1: the empty name breaks the log\n", encoding="utf-8"
    )

    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        {},
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
        {},
        _stages(),
        "TODO-0001",
        "implement",
        source="review",
    )
    assert rc == 1
    assert "REVIEW-фикс" not in (
        project / ".agentic" / "inbox" / "TODO-0001.md"
    ).read_text(encoding="utf-8")


def test_a_missing_target_is_refused(tmp_path):
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "findings", encoding="utf-8"
    )
    idx, todo, rc = pipeline_engine._handle_fix_round(
        project,
        project / ".agentic" / "logs",
        {},
        _stages(),
        "TODO-0001",
        "nope",
        source="review",
    )
    assert rc == 1
