"""Unit tests for awf.api.dispatch + awf.api.context (dogfood-2 automation).

dispatch_todo: atomic TODO creation + baseline + signal in one call.
load_supervisor_context: aggregate payload for one-shot bootstrap.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

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


# ─── load_supervisor_context ────────────────────────────────────────────


class TestLoadSupervisorContext:
    def test_returns_aggregate_payload(self, awf_project):
        """All fields populated from one call."""
        result = api.load_supervisor_context(awf_project)
        assert isinstance(result, api.SupervisorContextResult)
        assert result.project_name == "Test"
        assert result.plan_md  # init_project wrote a stub
        assert result.supervisor_md  # init_project copied template
        # Pipeline not running → these are None/False
        assert result.pipeline_running is False
        assert result.pipeline_pid is None

    def test_includes_vision_excerpt(self, awf_project):
        """Vision file detected and excerpt included."""
        (awf_project / "1. PRODUCT-VISION.md").write_text(
            "# Vision\nBuild the next big thing"
        )
        result = api.load_supervisor_context(awf_project)
        assert result.vision_path is not None
        assert "next big thing" in result.vision_excerpt

    def test_no_vision_emits_warning(self, awf_project):
        """Missing vision → warnings list has entry."""
        (awf_project / "README.md").unlink()  # init created README as vision
        result = api.load_supervisor_context(awf_project)
        assert any("Vision" in w for w in result.warnings)

    def test_next_stage_role_from_pipeline(self, awf_project):
        """next_stage_role extracted from pipeline.yaml when no current stage."""
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
        result = api.load_supervisor_context(awf_project)
        # Pipeline not started → first execute stage role
        assert result.next_stage_role == "developer"

    def test_role_prohibitions_extracted(self, awf_project):
        """When next_stage_role has prohibitions section in .md, it's returned."""
        pipelines = awf_project / ".agentic" / "pipelines"
        pipelines.mkdir(exist_ok=True)
        (pipelines / "default.yaml").write_text(yaml.safe_dump({
            "stages": [
                {"name": "plan", "role": "supervisor"},
                {"name": "reqs", "role": "system-analyst"},
            ],
        }))
        roles = awf_project / ".agentic" / "roles"
        (roles / "system-analyst.md").write_text(textwrap.dedent("""
            # ROLE: system-analyst

            ## Prohibitions (DO NOT do this)

            - DO NOT write code.
            - DO NOT make architecture decisions.
            - DO NOT pick libraries.

            ## Out of scope

            Implementation work belongs to developer role.
        """).strip())
        result = api.load_supervisor_context(awf_project)
        assert result.next_stage_role == "system-analyst"
        assert result.next_role_prohibitions is not None
        assert "DO NOT write code" in result.next_role_prohibitions
        assert "DO NOT make architecture" in result.next_role_prohibitions

    def test_missing_role_prohibitions_emits_warning(self, awf_project):
        """Role has no prohibitions section → warning."""
        pipelines = awf_project / ".agentic" / "pipelines"
        pipelines.mkdir(exist_ok=True)
        (pipelines / "default.yaml").write_text(yaml.safe_dump({
            "stages": [{"name": "x", "role": "mystery-role"}],
        }))
        result = api.load_supervisor_context(awf_project)
        # Role .md missing → next_role_prohibitions is None, warning emitted
        assert result.next_role_prohibitions is None
        assert any("mystery-role" in w and "prohibitions" in w for w in result.warnings)

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.load_supervisor_context(tmp_git_repo)

    def test_as_dict_serializable(self, awf_project):
        import json
        result = api.load_supervisor_context(awf_project)
        json.dumps(result.as_dict())


# ─── _extract_stage_info (dogfood-3 fix) ────────────────────────────────


class TestExtractStageInfo:
    """Dogfood-3 regression: real orchestrator log format is
    'Stage N/M: <name> (<role> :: <kind>)' — original markers
    '=== stage:', 'Pipeline stage:', 'Entering stage:' did NOT match.
    current_stage_name was always None.
    """

    def _write_log(self, project_dir: Path, lines: list[str]) -> None:
        logs = project_dir / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "awf-start.out").write_text("\n".join(lines) + "\n")

    def test_parses_real_orchestrator_format(self, awf_project):
        """Format: '  Stage 2/3: agent-system-analyst (...)'"""
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "=== Agentic Workflow: Starting Pipeline ===",
            "Stages: plan agent-system-analyst verify",
            "  Stage 1/3: plan (supervisor :: plan)",
            "BD-36: checkpoint decision=approve",
            "  Stage 2/3: agent-system-analyst (agent-system-analyst :: execute)",
        ])
        cur, _nxt, _sig, _, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert cur == "agent-system-analyst"

    def test_extracts_last_signal(self, awf_project):
        """Last 'BD-30: ... signal detected: TODO-NNNN' → last_signal field."""
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "  Stage 1/3: plan (supervisor :: plan)",
            "BD-30: interactive supervisor signal detected: TODO-0001",
            "  Stage 2/3: agent-system-analyst (... :: execute)",
        ])
        _cur, _nxt, sig, _, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert sig == "TODO-0001"

    def test_extracts_done_signal(self, awf_project):
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "  Stage 2/3: agent-system-analyst (... :: execute)",
            "Signal received: DONE-TODO-0001.ready",
        ])
        _cur, _nxt, sig, _, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert sig == "DONE-TODO-0001"

    def test_extracts_blocked_signal(self, awf_project):
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "  Stage 2/3: agent-system-analyst (... :: execute)",
            "BLOCKED signal detected: BLOCKED-TODO-0001.ready",
        ])
        _cur, _nxt, sig, _, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert sig == "BLOCKED-TODO-0001"

    def test_no_log_returns_none(self, awf_project):
        """No log file → all None, no crash."""
        from awf.api.context import _extract_stage_info

        cur, nxt, sig, log_tail, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert cur is None
        assert sig is None
        assert log_tail is None

    def test_log_tail_returned(self, awf_project):
        """log_tail field populated (last 30 lines)."""
        from awf.api.context import _extract_stage_info

        lines = [f"line {i}" for i in range(50)]
        self._write_log(awf_project, lines)
        _cur, _nxt, _sig, log_tail, _cp, _cpp, _cfurl = _extract_stage_info(awf_project)
        assert log_tail is not None
        assert "line 49" in log_tail
        assert "line 10" not in log_tail  # truncated

    def test_extracts_checkpoint_form_url(self, awf_project):
        """Dogfood-8: form_url extracted from log so supervisor can show it to user."""
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "  Stage 1/3: plan (supervisor :: plan)",
            "BD-36: checkpoint opened for TODO-0001 on port 42123",
            "BD-36: form_url=file:///tmp/awf-checkpoint-TODO-0001-abc.html",
            "BD-36: server_url=http://127.0.0.1:42123",
        ])
        _cur, _nxt, _sig, _lt, cp_pending, cp_port, cp_url = _extract_stage_info(awf_project)
        assert cp_pending is True
        assert cp_port == 42123
        assert cp_url == "file:///tmp/awf-checkpoint-TODO-0001-abc.html"

    def test_checkpoint_form_url_cleared_after_decision(self, awf_project):
        """After approve/edit/reject — checkpoint_pending=False (form_url may persist in log history)."""
        from awf.api.context import _extract_stage_info

        self._write_log(awf_project, [
            "  Stage 1/3: plan (supervisor :: plan)",
            "BD-36: checkpoint opened for TODO-0001 on port 42123",
            "BD-36: form_url=file:///tmp/awf-checkpoint-TODO-0001-abc.html",
            "BD-36: checkpoint decision=approve",
        ])
        _cur, _nxt, _sig, _lt, cp_pending, _cp_port, _cp_url = _extract_stage_info(awf_project)
        assert cp_pending is False
