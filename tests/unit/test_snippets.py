"""Tests for stage-specific snippet injection in build_prompt.

Validates that:
- Each stage kind (plan/verify/salvage) gets its focused snippet
- Execute stages do NOT get supervisor snippets (they get signal contract)
- Snippets contain key imperatives from dogfood findings
- _SNIPPET_ALWAYS is injected for plan/verify/salvage
- Snippets are NOT injected for execute stages
- Salvage prompt differs from verify (different context)
- get_salvage_snippet() returns public salvage instructions
"""
from __future__ import annotations

from awf.supervisor import (
    _SNIPPET_ALWAYS,
    _SNIPPET_PLAN,
    _SNIPPET_SALVAGE,
    _SNIPPET_VERIFY,
    _stage_snippet,
    build_prompt,
    get_salvage_snippet,
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

    def test_verify_has_handoffs(self):
        assert "handoff" in _SNIPPET_VERIFY.lower()

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


class TestBuildPromptInjection:
    """build_prompt appends snippets for supervisor stages."""

    def test_plan_prompt_has_always_snippet(self):
        prompt = build_prompt("plan", "TODO-0001")
        assert "Critical rules" in prompt
        assert "DO NOT edit" in prompt

    def test_plan_prompt_has_plan_snippet(self):
        prompt = build_prompt("plan", "TODO-0001")
        assert "Plan stage" in prompt

    def test_verify_prompt_has_verify_snippet(self):
        prompt = build_prompt("verify", "TODO-0001")
        assert "Verify stage" in prompt
        assert "DECIDE" in prompt or "decide" in prompt.lower()

    def test_verify_prompt_has_todo_id(self):
        prompt = build_prompt("verify", "TODO-0042")
        assert "TODO-0042" in prompt

    def test_salvage_prompt_has_salvage_snippet(self):
        prompt = build_prompt("salvage", "TODO-0001")
        assert "Salvage" in prompt

    def test_salvage_prompt_differs_from_verify(self):
        """Salvage and verify are different contexts — prompts must differ."""
        salvage = build_prompt("salvage", "TODO-0001")
        verify = build_prompt("verify", "TODO-0001")
        assert salvage != verify
        assert "SALVAGE" in salvage
        assert "SALVAGE" not in verify

    def test_execute_prompt_no_supervisor_snippet(self):
        """Execute stages get signal contract, NOT supervisor snippets."""
        prompt = build_prompt("execute", "TODO-0001")
        assert "Plan stage" not in prompt
        assert "Verify stage" not in prompt
        assert "Critical rules" not in prompt
        # But signal contract IS there (DF5-1)
        assert "DONE-TODO-0001.ready" in prompt

    def test_execute_prompt_has_output_discipline(self):
        """dogfood-11: every worker prompt teaches incremental file writes."""
        prompt = build_prompt("execute", "TODO-0001")
        assert "OUTPUT DISCIPLINE" in prompt
        assert "write/edit tools" in prompt
        assert "skeleton first" in prompt
        assert "STOP, save what you have" in prompt

    def test_snippet_at_end_of_prompt(self):
        """Stage-specific snippet should be towards the END of the prompt (recency bias)."""
        prompt = build_prompt("verify", "TODO-0001")
        verify_pos = prompt.rfind("Verify stage")
        # Stage-specific snippet should be in the last half
        half = len(prompt) / 2
        assert verify_pos > half, (
            f"Verify snippet at pos {verify_pos}, prompt len {len(prompt)}, "
            f"should be in last half (> {half:.0f})"
        )


class TestGetSalvageSnippet:
    """Public API for orchestrator salvage path."""

    def test_returns_string(self):
        result = get_salvage_snippet()
        assert isinstance(result, str)

    def test_contains_always_rules(self):
        result = get_salvage_snippet()
        assert "Critical rules" in result

    def test_contains_salvage_instructions(self):
        result = get_salvage_snippet()
        assert "Salvage" in result
        assert "git diff" in result.lower()

    def test_todo_id_substitution(self):
        result = get_salvage_snippet("TODO-0099")
        assert "TODO-0099" in result


class TestWorkspaceDiscipline:
    """SPEC-2: temp-path canon and live-run policy in every worker prompt."""

    def test_execute_prompt_has_workspace_discipline(self):
        prompt = build_prompt("execute", "TODO-0001")
        assert "WORKSPACE DISCIPLINE" in prompt
        assert "/tmp/opencode" in prompt
        assert "Live runs of external processes" in prompt
