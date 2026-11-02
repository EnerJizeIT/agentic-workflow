"""Unit tests for awf.api.dispatch + awf.api.context (dogfood-2 automation).

dispatch_todo: atomic TODO creation + baseline + signal in one call.
awf.brief / awf.get_status: the supervisor bootstrap surfaces.
"""
from __future__ import annotations

import pytest
import yaml

from awf import api


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


# ─── _next_todo_id ──────────────────────────────────────────────────────


class TestNextTodoId:
    def test_empty_project_returns_0001(self, awf_project):
        tid = api.dispatch._next_todo_id(awf_project)
        assert tid == "TODO-0001"

    def test_picks_max_plus_one(self, awf_project):
        (awf_project / ".agentic" / "inbox" / "TODO-0001.md").write_text("a")
        (awf_project / ".agentic" / "inbox" / "TODO-0003.md").write_text("b")
        tid = api.dispatch._next_todo_id(awf_project)
        assert tid == "TODO-0004"

    def test_scans_outbox_too_avoids_collision(self, awf_project):
        """Completed TODOs in outbox also counted — no collision."""
        outbox = awf_project / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "TODO-0007.md").write_text("done")
        tid = api.dispatch._next_todo_id(awf_project)
        assert tid == "TODO-0008"

    def test_ignores_non_numeric_suffixes(self, awf_project):
        """TODO-foo or TODO-XYZ must not break parsing."""
        (awf_project / ".agentic" / "inbox" / "TODO-XYZ.md").write_text("a")
        tid = api.dispatch._next_todo_id(awf_project)
        assert tid == "TODO-0001"


# ─── dispatch_todo ──────────────────────────────────────────────────────


class TestDispatchTodo:
    def test_creates_todo_baseline_and_signal(self, awf_project):
        """One call = TODO.md + BASELINE.sha + TODO.ready."""
        result = api.dispatch_todo(
            awf_project, "# TODO-0001\nDo the thing"
        )
        assert isinstance(result, api.DispatchTodoResult)
        assert result.todo_id == "TODO-0001"
        assert len(result.baseline_sha) == 40  # git SHA-1 hex

        # Files exist
        inbox = awf_project / ".agentic" / "inbox"
        context = awf_project / ".agentic" / "context"
        assert (inbox / "TODO-0001.md").is_file()
        assert (inbox / "TODO-0001.ready").is_file()
        assert (context / "BASELINE-TODO-0001.sha").is_file()

        # files_written lists all
        assert ".agentic/inbox/TODO-0001.md" in result.files_written
        assert ".agentic/inbox/TODO-0001.ready" in result.files_written
        assert any("BASELINE-TODO-0001" in f for f in result.files_written)

    def test_role_hint_added_as_html_comment(self, awf_project):
        """role= parameter stored as HTML comment in .md (invisible to LLM)."""
        api.dispatch_todo(
            awf_project, "# Task\nDo work", role="agent-system-analyst"
        )
        md = (awf_project / ".agentic" / "inbox" / "TODO-0001.md").read_text()
        assert "<!-- role_hint: agent-system-analyst -->" in md

    def test_explicit_todo_id_overrides_auto(self, awf_project):
        """todo_id= overrides auto-generated NNNN."""
        result = api.dispatch_todo(
            awf_project, "# Special", todo_id="TODO-0042"
        )
        assert result.todo_id == "TODO-0042"
        assert (awf_project / ".agentic" / "inbox" / "TODO-0042.md").exists()

    def test_invalid_todo_id_format_rejected(self, awf_project):
        """todo_id must match TODO-NNNN pattern."""
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.dispatch_todo(awf_project, "x", todo_id="TODO-foo")
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.dispatch_todo(awf_project, "x", todo_id="task-0001")

    def test_empty_content_rejected(self, awf_project):
        """Empty body rejected — would create useless TODO."""
        with pytest.raises(api.AwfApiError, match="content is required"):
            api.dispatch_todo(awf_project, "")
        with pytest.raises(api.AwfApiError, match="content is required"):
            api.dispatch_todo(awf_project, "   \n  ")

    def test_collision_with_existing_todo_rejected(self, awf_project):
        """Cannot overwrite existing TODO-NNNN.md via dispatch."""
        (awf_project / ".agentic" / "inbox" / "TODO-0001.md").write_text("existing")
        with pytest.raises(api.AwfApiError, match="already exists"):
            api.dispatch_todo(
                awf_project, "new", todo_id="TODO-0001"
            )

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.dispatch_todo(tmp_git_repo, "content")

    def test_as_dict_serializable(self, awf_project):
        """Result.as_dict() JSON-serializable for MCP."""
        import json
        result = api.dispatch_todo(awf_project, "x")
        json.dumps(result.as_dict())


# ─── brief / status: the bootstrap surfaces after IMP1 ─────────────────


class TestBootstrapSurfaces:
    def test_brief_aggregate_payload(self, awf_project):
        """The card is the single entry point; its basics come from one call."""
        result = api.brief(awf_project)
        assert result.project == "Test"
        assert result.phase
        assert result.text
        assert result.pipeline_running is False
        assert result.pipeline_pid is None

    def test_next_stage_role_from_pipeline(self, awf_project):
        """next_stage_role extracted from pipeline.yaml when no current stage."""
        from awf.api.context import _extract_stage_info

        pipelines = awf_project / ".agentic" / "pipelines"
        pipelines.mkdir(exist_ok=True)
        (pipelines / "default.yaml").write_text(yaml.safe_dump({
            "name": "default",
            "stages": [
                {"name": "plan", "role": "supervisor"},
                {"name": "implement", "role": "developer"},
                {"name": "verify", "role": "supervisor"},
            ],
        }))
        _stage, next_role, _signal, _tail, *_rest = _extract_stage_info(
            awf_project
        )
        # Pipeline not started → first execute stage role
        assert next_role == "developer"

    def test_as_dict_serializable(self, awf_project):
        import json
        result = api.brief(awf_project)
        json.dumps(result.as_dict())


# AUD-12.5: _extract_stage_info_regex removed (state file always written since T4.1).
# Regex-specific tests removed. State-file-based tests in test_pipeline_state.py.
