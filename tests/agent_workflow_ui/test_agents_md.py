"""RUN6 #5 (TODO-0060): the plugin's system prompt (AGENTS.md block).

The supervisor onboarding text used to live ONLY in the installed
``~/.config/opencode/AGENTS.md`` — no source in the repo, no tests.
``agents_md.py`` is the single source of truth; ``ensure_agents_md``
keeps the installed copy in sync on every plugin start.
"""
from __future__ import annotations

from agent_workflow_ui.agents_md import (
    BLOCK,
    END_MARKER,
    START_MARKER,
    block_with_markers,
    ensure_agents_md,
    patch_agents_md,
)


class TestPatchAgentsMd:
    def test_empty_file_gets_block(self):
        out = patch_agents_md("")
        assert START_MARKER in out
        assert END_MARKER in out
        assert "awf_brief" in out

    def test_appends_when_markers_absent(self):
        other = "# Other plugin\n\nsome content\n"
        out = patch_agents_md(other)
        assert out.startswith(other)
        assert out.count(START_MARKER) == 1

    def test_replaces_block_in_place(self):
        old = block_with_markers().replace("awf_brief", "OLD_JUMP_TEXT")
        text = "# Header\n" + old + "# Tail\n"
        out = patch_agents_md(text)
        assert out.startswith("# Header\n")
        assert out.endswith("# Tail\n")
        assert out.count(START_MARKER) == 1
        assert "awf_brief" in out
        assert "OLD_JUMP_TEXT" not in out

    def test_idempotent(self):
        once = patch_agents_md("")
        assert patch_agents_md(once) == once

    def test_preserves_other_plugins(self):
        other = (
            "<!-- codebase-memory-mcp:start -->\ngraph stuff\n"
            "<!-- codebase-memory-mcp:end -->\n"
        )
        out = patch_agents_md(other)
        assert "graph stuff" in out
        assert "codebase-memory-mcp" in out


class TestOnboardingLine:
    def test_brief_is_the_entry_point(self):
        # RUN6 #5: a new session starts with awf_brief, not a jump to
        # awf_status (mid-cycle state check only).
        assert "start with `awf_brief`" in BLOCK
        assert "jump straight" not in BLOCK

    def test_skill_and_docs_referenced(self):
        assert "awf-supervisor" in BLOCK
        assert "USAGE.md" in BLOCK

    def test_no_stale_polling_advice(self):
        # The old rule told the supervisor to poll — R6 sleep mode forbids it.
        assert "Poll awf_status" not in BLOCK
        assert "Never poll in a loop" in BLOCK


class TestEnsureAgentsMd:
    def test_writes_and_is_idempotent(self, tmp_path):
        target = tmp_path / "AGENTS.md"
        assert ensure_agents_md(target) is True
        first = target.read_text(encoding="utf-8")
        assert START_MARKER in first
        assert ensure_agents_md(target) is True
        assert target.read_text(encoding="utf-8") == first

    def test_updates_drifted_block(self, tmp_path):
        target = tmp_path / "AGENTS.md"
        target.write_text(
            patch_agents_md("").replace("awf_brief", "DRIFTED_TEXT"),
            encoding="utf-8",
        )
        assert ensure_agents_md(target) is True
        text = target.read_text(encoding="utf-8")
        assert "awf_brief" in text
        assert "DRIFTED_TEXT" not in text

    def test_missing_parent_created(self, tmp_path):
        target = tmp_path / "nested" / "AGENTS.md"
        assert ensure_agents_md(target) is True
        assert target.is_file()
