"""A32/A34 + IMP2b: the replan prompt through the live stage path.

The old ``rejection.build_replan_context`` is gone (QA-10): these tests call
``supervisor._prepare_supervisor_stage`` — the text the engine spawns — and
check the three sources it must carry (findings, TODO, BLOCKED), the
``replan_for`` marker the wait keys on, and the 6 000 cap.
"""
from __future__ import annotations

from pathlib import Path

from awf import supervisor


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "handoff"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  replan: supervisor\n  verify: supervisor\n",
        encoding="utf-8",
    )
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO — the unit\n\nGoal: keep it small.\n", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / "REVIEW-REJECTED-TODO-0001.md").write_text(
        "# REVIEW-REJECTED\n\n- CTX9: the report the worker must read\n",
        encoding="utf-8",
    )
    (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").write_text(
        "# BLOCKED\n\nreason-marker\n", encoding="utf-8"
    )
    (project / ".agentic" / "roles").mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "roles" / "supervisor.md").write_text(
        "# Supervisor\n", encoding="utf-8"
    )
    return project


def _handoff(project: Path, stage: str, text: str, mtime: int) -> Path:
    path = project / ".agentic" / "handoff" / f"{stage}-TODO-0001.md"
    path.write_text(text, encoding="utf-8")
    import os

    os.utime(path, (mtime, mtime))
    return path


def _prompt(project: Path) -> str:
    prompt = supervisor._prepare_supervisor_stage(
        "replan", "TODO-0001", project, project / ".agentic" / "logs"
    )
    assert prompt is not None
    return prompt


def test_the_prompt_carries_the_reviewer_report(tmp_path):
    prompt = _prompt(_project(tmp_path))

    assert "CTX9: the report the worker must read" in prompt


def test_handoff_history_never_enters_the_prompt(tmp_path):
    project = _project(tmp_path)
    _handoff(project, "plan", "PLAN-HISTORY-MARKER", 1_000_000)
    _handoff(project, "agent-implementer", "IMPLEMENTER-HISTORY-MARKER", 2_000_000)

    prompt = _prompt(project)

    assert "PLAN-HISTORY-MARKER" not in prompt
    assert "IMPLEMENTER-HISTORY-MARKER" not in prompt


def test_the_prompt_names_the_replan_marker(tmp_path):
    assert "replan_for: TODO-0001" in _prompt(_project(tmp_path))


def test_the_prompt_carries_the_blocked_fact_before_the_contract(tmp_path):
    prompt = _prompt(_project(tmp_path))

    assert "reason-marker" in prompt
    assert prompt.rstrip().endswith("молчание: провал.")


def test_the_prompt_never_exceeds_the_supervisor_cap(tmp_path):
    project = _project(tmp_path)
    todo = project / ".agentic" / "inbox" / "TODO-0001.md"
    todo.write_text(todo.read_text(encoding="utf-8") + "\n" + "x" * 20000,
                    encoding="utf-8")

    prompt = _prompt(project)

    assert len(prompt) <= 6000


def test_the_replan_stage_returns_text_not_files(tmp_path):
    """The live path hands the spawner a prompt, not --file attachments."""
    project = _project(tmp_path)
    prompt = _prompt(project)
    assert isinstance(prompt, str) and prompt
