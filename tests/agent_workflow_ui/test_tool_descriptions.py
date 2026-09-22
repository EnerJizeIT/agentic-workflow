"""RUN6 #5 (TODO-0060): MCP tool descriptions — what the model reads first.

Contract: every registered tool has a non-empty, one-line-first description
(no placeholders). The weak descriptions fixed in RUN6 #5 are spot-checked
(verb-first one-liners).
"""
from __future__ import annotations

import pytest

pytest.importorskip("mcp.server.fastmcp")

from agent_workflow_ui.server import create_server


@pytest.fixture(scope="module")
def tools():
    return create_server()._tool_manager._tools


def _first_line(desc: str) -> str:
    return (desc or "").strip().splitlines()[0]


class TestAllTools:
    def test_every_tool_has_a_description(self, tools):
        missing = [n for n, t in tools.items() if not (t.description or "").strip()]
        assert missing == []

    def test_no_placeholder_descriptions(self, tools):
        bad = []
        for name, t in tools.items():
            low = _first_line(t.description).lower()
            if "placeholder" in low or "tbd" in low or "fixme" in low or low.startswith("todo"):
                bad.append(name)
        assert bad == []


class TestRun6VerbFirstFixes:
    """The RUN6 #5 rewrites: one line, verb-first, says what the tool does."""

    @pytest.mark.parametrize(
        ("tool", "verb"),
        [
            ("awf_brief", "Show"),
            ("awf_run_status", "Show"),
            ("awf_run_note", "Set"),
            ("awf_tree_sha", "Compute"),
            ("awf_prove_red", "Prove"),
            ("awf_verify_pack", "Produce"),
            ("awf_metrics", "Collect"),
            ("awf_feedback", "Write"),
            ("awf_reset", "Clean"),
            ("awf_rollback", "Roll"),
            ("awf_dispatch_todo", "Create"),
        ],
    )
    def test_verb_first(self, tools, tool, verb):
        first = _first_line(tools[tool].description)
        assert first.startswith(verb), f"{tool}: {first!r}"

    def test_brief_description_names_the_card(self, tools):
        d = tools["awf_brief"].description
        assert "onboarding" in d
        assert "tool map" in d

    def test_reset_description_marks_destructive(self, tools):
        assert "destructive" in tools["awf_reset"].description.lower()
