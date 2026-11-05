"""Tests for stage-specific snippet injection in the supervisor prompt.

Validates that:
- Each stage kind (plan/verify/salvage) gets its focused snippet
- Execute stages do NOT get supervisor snippets (they get signal contract)
- Snippets contain key imperatives from dogfood findings
- _SNIPPET_ALWAYS is injected for plan/verify/salvage
- Snippets are NOT injected for execute stages
- Salvage prompt differs from verify (different context)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.supervisor import (
    _SNIPPET_ALWAYS,
    _SNIPPET_PLAN,
    _SNIPPET_SALVAGE,
    _SNIPPET_VERIFY,
    _stage_snippet,
)


class TestSnippetContent:
    """Snippets contain the right imperatives."""

    def test_always_has_do_not_edit(self):
        assert "DO NOT edit" in _SNIPPET_ALWAYS

    def test_always_has_decide_yourself(self):
        assert "decision maker" in _SNIPPET_ALWAYS.lower() or "decide yourself" in _SNIPPET_ALWAYS.lower()

    def test_always_has_bash_fallback(self):
        assert "bash" in _SNIPPET_ALWAYS.lower() or "fallback" in _SNIPPET_ALWAYS.lower()

    def test_plan_has_create_todo(self):
        assert "TODO" in _SNIPPET_PLAN
        assert "ready" in _SNIPPET_PLAN.lower()

    def test_verify_has_decide(self):
        assert "DECIDE" in _SNIPPET_VERIFY or "decide" in _SNIPPET_VERIFY.lower()

    def test_verify_facts_not_handoffs(self):
        """A42: the verify prompt carries pack facts, not handoff history."""
        lower = _SNIPPET_VERIFY.lower()
        assert "handoff" not in lower
        assert "do not run tests" in lower
        assert "pack facts" in lower

    def test_verify_has_no_relay(self):
        assert "relay" in _SNIPPET_VERIFY.lower() or "YOUR call" in _SNIPPET_VERIFY

    def test_salvage_has_git_diff(self):
        assert "git diff" in _SNIPPET_SALVAGE.lower()

    def test_salvage_has_ack(self):
        assert "ACK" in _SNIPPET_SALVAGE

    def test_salvage_has_repeat_escalation(self):
        """dogfood-11: repeat salvage → split the task, don't blind-retry."""
        assert "REPEAT salvage" in _SNIPPET_SALVAGE
        lower = _SNIPPET_SALVAGE.lower()
        assert "split the work" in lower
        assert "output budget" in lower

    def test_verify_prompt_has_metrics(self):
        """U8c: the supervisor's prompts point to awf_metrics (the ALWAYS
        snippet is injected into every supervisor stage prompt)."""
        assert "awf_metrics" in _SNIPPET_ALWAYS
        assert "awf metrics" in _SNIPPET_ALWAYS
        assert "metrics.mirror_dir" in _SNIPPET_ALWAYS

    def test_snippets_are_concise(self):
        """Each snippet should be under 30 lines (focused, not a wall of text)."""
        for name, snippet in [("always", _SNIPPET_ALWAYS), ("plan", _SNIPPET_PLAN),
                              ("verify", _SNIPPET_VERIFY), ("salvage", _SNIPPET_SALVAGE)]:
            line_count = len(snippet.strip().splitlines())
            assert line_count <= 30, f"{name} snippet is {line_count} lines (should be <=30)"


class TestStageSnippetLoader:
    """_stage_snippet returns the right snippet per kind."""

    def test_plan_returns_plan_snippet(self):
        result = _stage_snippet("plan")
        assert "Plan stage" in result

    def test_verify_returns_verify_snippet(self):
        result = _stage_snippet("verify")
        assert "Verify stage" in result

    def test_salvage_returns_salvage_snippet(self):
        result = _stage_snippet("salvage")
        assert "Salvage" in result

    def test_execute_returns_empty(self):
        result = _stage_snippet("execute")
        assert result == ""

    def test_unknown_returns_empty(self):
        result = _stage_snippet("unknown_kind")
        assert result == ""

    def test_todo_id_substitution(self):
        result = _stage_snippet("verify", "TODO-0042")
        assert "TODO-0042" in result


# ─── B (run2 report): supervisor authority phrase in the shipped copy ──────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

SUPERVISOR_TEMPLATE_COPIES = ("awf/templates/roles/supervisor.md",)
PHASE_RUN_TEMPLATE_COPIES = ("awf/templates/roles/supervisor/phase-run.md",)


def _read_template(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


class TestSupervisorAuthorityPhrase:
    """Owner-confirmed right (run2 report): replan / spec rewrite / split
    is a standard supervisor option — no owner approval. The phrase must
    live in the shipped template (the only copy since TODO-0230 Y1)."""

    @pytest.mark.parametrize("rel", SUPERVISOR_TEMPLATE_COPIES)
    def test_supervisor_md_has_authority_phrase(self, rel):
        text = _read_template(rel)
        assert "Replanning is your standard option" in text
        assert "does NOT require owner approval" in text
        assert "stop-list" in text

    @pytest.mark.parametrize("rel", PHASE_RUN_TEMPLATE_COPIES)
    def test_phase_run_has_authority_phrase(self, rel):
        text = _read_template(rel)
        assert "standard option" in text
        assert "owner approval" in text
        assert "stop-list" in text


def test_supervisor_prompt_carries_always_and_kind_snippet(tmp_path):
    """IMP2b: the snippets are the tail of the built stage text."""
    from awf import supervisor
    from tests.unit._signal_fakes import make_project

    proj = make_project(tmp_path)
    prompt = supervisor._prepare_supervisor_stage(
        "verify", "TODO-0001", proj, proj / ".agentic" / "logs"
    )
    assert prompt is not None
    assert "Critical rules" in prompt
    assert "Verify stage" in prompt
