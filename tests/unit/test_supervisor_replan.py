"""A34: the replan prompt is bounded and carries only what it needs.

The 01.11 run died because the replan subprocess got the whole handoff
history and the provider answered HTTP 400. The prompt now names three
sources — the reviewer's report, the current TODO, the report of the stage
that slid — never the handoff history or the DONE archive, and it is cut
before the spawn when it exceeds the engine's existing role-text budget.
"""
from __future__ import annotations

from pathlib import Path

from awf import supervisor


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "handoff"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: A34\n")
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO — the unit\n\nGoal: keep it small.\n", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "# REVIEW\n\n- CTX9: the report the worker must read\n", encoding="utf-8"
    )
    return project


def _handoff(project: Path, stage: str, text: str, mtime: int) -> Path:
    path = project / ".agentic" / "handoff" / f"{stage}-TODO-0001.md"
    path.write_text(text, encoding="utf-8")
    import os

    os.utime(path, (mtime, mtime))
    return path


def test_the_prompt_carries_the_reviewer_report_and_the_todo(tmp_path):
    project = _project(tmp_path)

    prompt, dropped = supervisor.build_replan_context("TODO-0001", project)

    assert "CTX9: the report the worker must read" in prompt
    assert "Goal: keep it small." in prompt
    assert dropped == []


def test_handoff_history_never_enters_the_prompt(tmp_path):
    """Only the newest handoff is the stage report; the rest is history."""
    project = _project(tmp_path)
    _handoff(project, "plan", "PLAN-HISTORY-MARKER", 1_000_000)
    _handoff(project, "agent-implementer", "IMPLEMENTER-HISTORY-MARKER", 2_000_000)
    _handoff(project, "agent-architect-review", "ARCH-REVIEW-HISTORY-MARKER", 3_000_000)
    _handoff(project, "agent-qa-review", "QA-STAGE-REPORT", 4_000_000)

    prompt, _dropped = supervisor.build_replan_context("TODO-0001", project)

    assert "PLAN-HISTORY-MARKER" not in prompt
    assert "IMPLEMENTER-HISTORY-MARKER" not in prompt
    assert "ARCH-REVIEW-HISTORY-MARKER" not in prompt
    assert "QA-STAGE-REPORT" in prompt


def test_only_the_newest_stage_report_is_a_source(tmp_path):
    project = _project(tmp_path)
    _handoff(project, "plan", "OLD-STAGE-REPORT", 1_000_000)
    _handoff(project, "review", "NEW-STAGE-REPORT", 2_000_000)

    prompt, _dropped = supervisor.build_replan_context("TODO-0001", project)

    assert "NEW-STAGE-REPORT" in prompt
    assert "OLD-STAGE-REPORT" not in prompt


def test_the_prompt_never_exceeds_the_role_budget(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _handoff(project, "review", "S" * 2000, 2_000_000)
    monkeypatch.setattr(supervisor, "_replan_budget_chars", lambda: 300)

    prompt, dropped = supervisor.build_replan_context(
        "TODO-0001", project, project / ".agentic" / "logs"
    )

    assert len(prompt) <= 300
    assert "stage report" in dropped
    log = (project / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "replan prompt over 300 chars" in log


def test_a_large_todo_yields_before_the_closing_instruction(tmp_path, monkeypatch):
    """A34: without a reviewer report the TODO is cut, not the tail.

    The cut order is "TODO first, then the reviewer's report". A positional
    walk skipped the only section when the report was missing, so the
    overflow came off the END of the prompt — the ``replan_for`` closing
    instruction. The supervisor then wrote an answer the engine's wait
    could not recognize (``Supervisor did not answer the escalation``).
    """
    project = _project(tmp_path)
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").unlink()
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO — the unit\n\n" + ("LONG-TODO " * 1000), encoding="utf-8"
    )
    monkeypatch.setattr(supervisor, "_replan_budget_chars", lambda: 600)

    prompt, dropped = supervisor.build_replan_context("TODO-0001", project)

    assert len(prompt) <= 600
    assert "current TODO truncated" in dropped
    assert "replan_for: TODO-0001" in prompt


def test_a_small_budget_still_keeps_the_reviewer_report(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setattr(supervisor, "_replan_budget_chars", lambda: 400)

    prompt, _dropped = supervisor.build_replan_context("TODO-0001", project)

    assert len(prompt) <= 400
    assert "CTX9" in prompt


def test_a_worker_spelled_report_still_enters_the_prompt(tmp_path):
    """A32: the worker's ``REVIEW-REJECTED-<id>.md`` is the same report.

    The replan used to look only at ``REVIEW-<id>.md``; a worker rejection
    would then push a prompt with no findings into the supervisor.
    """
    project = _project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / "REVIEW-TODO-0001.md").unlink()
    (outbox / "REVIEW-REJECTED-TODO-0001.md").write_text(
        "# Rejected\n\n- REJECTED-SPELLING-MARKER\n", encoding="utf-8"
    )

    prompt, _dropped = supervisor.build_replan_context("TODO-0001", project)

    assert "REJECTED-SPELLING-MARKER" in prompt


def test_the_prompt_head_and_tail_name_the_same_contract(tmp_path):
    """A32/A34: the replan path runs only under ``on_rejected: replan`` and
    the wait accepts only a NEW ``replan_for``-marked TODO.

    A head that says "refine the SAME unit (append, do not clone)" sends the
    supervisor to append the current unit, the wait sees no answer, and the
    run stops ("Supervisor did not answer the escalation"). The head must not
    contradict the closing instruction.
    """
    project = _project(tmp_path)

    prompt, _dropped = supervisor.build_replan_context("TODO-0001", project)
    head = prompt.splitlines()[0]

    assert "SAME unit" not in head
    assert "append" not in head.lower()
    assert "replan_for: TODO-0001" in prompt
    assert ".ready" in prompt


def test_the_replan_stage_passes_no_handoff_files(tmp_path):
    """The command line carries no handoff/DONE files — only the prompt."""
    project = _project(tmp_path)
    _handoff(project, "review", "REVIEW-STAGE-REPORT", 2_000_000)

    prepared = supervisor._prepare_supervisor_stage(
        "replan",
        "TODO-0001",
        project,
        {"project": {"name": "A34"}},
        ".agentic/phases/plan.md",
        project / ".agentic" / "logs",
        None,
    )

    assert prepared is not None
    extra_files, prompt = prepared
    assert extra_files == []
    assert ".agentic/handoff" not in prompt
    assert "REVIEW-STAGE-REPORT" in prompt
