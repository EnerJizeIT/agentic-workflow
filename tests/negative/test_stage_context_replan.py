"""Live supervisor stage prompts: replan and verify (IMP2b).

Split from ``test_stage_context_pack.py`` (ratchet budget): these tests go
through ``supervisor._prepare_supervisor_stage`` — the text the engine
really spawns — while the pack file owns the builder-level facts.
"""
from __future__ import annotations

import pytest

from awf.stage_context import build_stage_context
from tests.negative.test_stage_context_pack import (  # noqa: F401
    REPO,
    TODO_ID,
    TODO_TEXT,
    _project,
)


def _live_supervisor_prompt(tmp_path, kind: str, *, todo: str = TODO_TEXT):
    """The prompt the supervisor path really spawns with — not a direct build."""
    from awf import supervisor

    root = _project(tmp_path, todo=todo)
    prompt = supervisor._prepare_supervisor_stage(
        kind, TODO_ID, root, root / ".agentic" / "logs",
    )
    assert prompt is not None
    return root, prompt


def test_replan_prompt_names_the_replan_marker(tmp_path):
    """The engine's replan wait accepts only a ``replan_for``-marked TODO.

    ``supervisor.py::_replan_marker_of`` and the interactive wait skip every
    unmarked TODO: a replan prompt without this closing instruction can only
    time out ("Supervisor did not answer the escalation"). The pre-IMP2b
    builder carried it in its tail.
    """
    _root, prompt = _live_supervisor_prompt(tmp_path, "replan")

    assert f"replan_for: {TODO_ID}" in prompt


def test_replan_prompt_carries_the_reviewer_report(tmp_path):
    """A real outbox report has no ``## REVIEW-фикс`` heading.

    ``REVIEW-REJECTED-<id>.md`` is the reviewer's own file (heading
    ``# REVIEW-REJECTED``); the replan prompt must carry its findings, or
    the supervisor refines the unit blind.
    """
    without_rounds = TODO_TEXT.split("## REVIEW-фикс")[0]
    root, _prompt = _live_supervisor_prompt(tmp_path, "replan", todo=without_rounds)
    (root / ".agentic" / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md").write_text(
        "# REVIEW-REJECTED — sample\n\n- QA-marker: finding from the report\n",
        encoding="utf-8",
    )
    from awf import supervisor

    prompt = supervisor._prepare_supervisor_stage(
        "replan", TODO_ID, root, root / ".agentic" / "logs",
    )

    assert prompt is not None
    assert "QA-marker" in prompt


def test_replan_prompt_ends_with_the_output_contract(tmp_path):
    """AC22/T19: ``OUTPUT`` is last in every type; the blocker note is not tail.

    ``BLOCKED`` appended after ``OUTPUT`` puts a fact after the closing
    instruction — the stage reads "молчание: провал" before the reason it
    was woken for.
    """
    root = _project(tmp_path)
    (root / ".agentic" / "outbox" / f"BLOCKED-{TODO_ID}.md").write_text(
        "BLOCKED: reason-marker\n", encoding="utf-8",
    )
    from awf import supervisor

    prompt = supervisor._prepare_supervisor_stage(
        "replan", TODO_ID, root, root / ".agentic" / "logs",
    )

    assert prompt is not None
    assert "reason-marker" in prompt
    assert prompt.rstrip().endswith("молчание: провал.")


def test_verify_stage_prompt_is_capped_and_has_no_handoff_history(tmp_path):
    """The live verify path (not the builder) obeys the 6 000 cap and PREVLINK."""
    _root, prompt = _live_supervisor_prompt(tmp_path, "verify")

    assert len(prompt) <= 6000
    assert "HANDOFF-BODY" not in prompt


def test_replan_stage_prompt_fits_6000_and_has_no_handoff_history(tmp_path):
    """The live replan stage: cap, blocker fact in, handoff history out.

    The builder-level test above proves the blocks; this one proves the
    stage the engine actually spawns keeps the 6 000 cap, keeps the
    ``BLOCKED`` note before the closing contract, and never forwards a
    handoff body (``PREVLINK`` is excluded on this path).
    """
    root = _project(tmp_path)
    (root / ".agentic" / "outbox" / f"BLOCKED-{TODO_ID}.md").write_text(
        "BLOCKED: reason-marker\n", encoding="utf-8",
    )
    from awf import supervisor

    prompt = supervisor._prepare_supervisor_stage(
        "replan", TODO_ID, root, root / ".agentic" / "logs",
    )

    assert prompt is not None
    assert len(prompt) <= 6000
    assert "HANDOFF-BODY" not in prompt
    assert "reason-marker" in prompt
    assert prompt.index("reason-marker") < prompt.index("## Контракт вывода")


def test_real_execute_prompt_keeps_the_role_core():
    """Acceptance: the real unit's prompt still carries the role, not a link.

    With the completion contract in the suffix and a 40-file allowlist, the
    cut order spends the whole cap before ``ROLE``: the stage used to get
    ``… ещё N: см. файл`` and nothing else (``role_pct=1``). The role's
    obligations must survive on the project's own TODO.
    """
    todo = REPO / ".agentic" / "inbox" / "TODO-0383.md"
    role_file = REPO / ".agentic" / "roles" / "agent-implementer.md"
    if not todo.is_file() or not role_file.is_file():
        pytest.skip("real TODO/role are not in this checkout")

    from awf import agent_stage as agent_stage_mod
    from awf.pipeline import Stage

    stage = Stage(
        name="agent-implementer", role="agent-implementer", kind="execute",
        task="Implement the unit per its contract.",
    )
    s_id = stage.id or stage.name
    suffix = "\n\n".join(
        part
        for part in (
            agent_stage_mod._execute_contract(TODO_ID),
            agent_stage_mod._stage_assignment_block(stage),
            agent_stage_mod._build_pipeline_context(REPO, TODO_ID, None),
            "## Stage identity\nstage_id=\"agent-implementer\", attempt=1",
        )
        if part
    )
    result = build_stage_context(
        REPO, "TODO-0383", s_id, "agent-implementer", suffix=suffix,
    )
    role = next(block for block in result.blocks if block.id == "ROLE")
    review = next(block for block in result.blocks if block.id == "REVIEW")

    assert not role.full().startswith("… ещё"), role.full()
    assert role.taken == 1200, role.taken
    assert len(suffix) <= 1500
    assert review.full().strip(), "the findings block must not be dropped"
    assert "REVIEW" not in result.cut
