"""Unit tests for awf.api.load_supervisor_context (the full supervisor view).

ORCH M1.2: the run record fields (goal, criteria, position, budget,
decisions, sources, warning) and the run-aware next_action. The two-
views-of-one-record invariants on synthetic projects are pinned in the
negative layer (tests/negative/test_context_single_source.py); here the
plain unit behavior of the loader.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import api, run_state

T = "TODO-0001"


@pytest.fixture
def awf_project(tmp_git_repo: Path):
    """Project with .agentic/ initialized."""
    api.init_project(tmp_git_repo, project_name="CtxTest")
    return tmp_git_repo


class TestRunRecordFields:
    def test_outside_run_fields_are_empty(self, awf_project: Path) -> None:
        """No run: every run field is empty/False — the payload shape is
        stable for old callers (invariant 6)."""
        result = api.load_supervisor_context(awf_project)
        assert result.run_active is False
        assert result.run_goal == ""
        assert result.run_criteria == []
        assert result.run_position == "0/0"
        assert result.run_budget_minutes == 0
        assert result.run_budget_left_minutes == 0
        assert result.run_note == ""
        assert result.run_last_decision == ""
        assert result.run_decisions == []
        assert result.run_sources == []
        assert result.run_warning == ""
        assert "run.yaml" not in " ".join(result.warnings)

    def test_active_run_fields_populated(self, awf_project: Path) -> None:
        """An active run with a plan: goal/criteria/position/budget are
        read from the shared record."""
        api.run_start(
            awf_project,
            queue=[T],
            goal="Stabilize the run state",
            criteria=["gates green", "no second store"],
            budget_minutes=120,
        )
        result = api.load_supervisor_context(awf_project)
        assert result.run_active is True
        assert result.run_goal == "Stabilize the run state"
        assert result.run_criteria == ["gates green", "no second store"]
        assert result.run_position == "1/1"
        assert result.run_budget_minutes == 120
        # int truncation of the productive-elapsed minutes: a fresh run
        # shows 119..120 (same formula as run_brief)
        assert result.run_budget_left_minutes in (119, 120)
        assert ".agentic/state/run.yaml" in result.run_sources

    def test_decisions_carried_in_full(self, awf_project: Path) -> None:
        """The full view keeps every decision entry (which, why, when)."""
        api.run_start(awf_project, queue=[T])
        api.reject_commit(awf_project, T, "tests red: 3 failures")
        result = api.load_supervisor_context(awf_project)
        assert len(result.run_decisions) == 1
        d = result.run_decisions[0]
        assert d["kind"] == "reject"
        assert d["todo_id"] == T
        assert d["reason"] == "tests red: 3 failures"
        assert d["ts"]
        assert result.run_last_decision == f"reject {T} — tests red: 3 failures"
        assert f"outbox/REVIEW-{T}.md" in result.run_sources

    def test_next_action_run_aware_after_reject(self, awf_project: Path) -> None:
        """After a reject in a run the nearest permitted action is the run
        one (awf_run_next), not the generic awf_start."""
        api.run_start(awf_project, queue=[T])
        api.reject_commit(awf_project, T, "diff too wide")
        result = api.load_supervisor_context(awf_project)
        assert "awf_run_next" in result.next_action
        assert "awf_start" not in result.next_action

    def test_next_action_empty_outside_run(self, awf_project: Path) -> None:
        """No run (or no decision yet): the field is empty and the MCP
        surface keeps its phase hint (behavior unchanged)."""
        api.run_start(awf_project, queue=[T], goal="no decisions yet")
        result = api.load_supervisor_context(awf_project)
        assert result.next_action == ""

    def test_broken_run_yaml_degrades_with_warning(self, awf_project: Path) -> None:
        """A corrupted run.yaml degrades to "no run" with a warning in the
        payload — no traceback (invariant 5)."""
        f = run_state.run_file(awf_project)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("active: [unclosed", encoding="utf-8")
        result = api.load_supervisor_context(awf_project)
        assert result.run_warning
        assert "run.yaml" in result.run_warning
        assert any("run.yaml" in w for w in result.warnings)
        assert result.run_active is False
        assert result.run_goal == ""
        assert result.run_decisions == []

    def test_brief_and_context_share_the_record(self, awf_project: Path) -> None:
        """The same reading on both surfaces: the card's compact fields
        equal the full view's (clipping aside)."""
        api.run_start(awf_project, queue=[T], goal="One record, two views")
        api.reject_commit(awf_project, T, "missing test")
        b = api.brief(awf_project)
        c = api.load_supervisor_context(awf_project)
        assert b.run_goal == c.run_goal
        assert b.run_last_decision == c.run_last_decision
        assert b.run_sources == c.run_sources
        assert b.run_warning == c.run_warning


class TestPayloadShape:
    def test_as_dict_serializable_with_run_fields(self, awf_project: Path) -> None:
        """The new fields survive the JSON roundtrip (MCP response)."""
        api.run_start(awf_project, queue=[T], goal="json roundtrip")
        result = api.load_supervisor_context(awf_project)
        payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))
        assert payload["run_goal"] == "json roundtrip"
        assert payload["run_active"] is True
        assert payload["run_decisions"] == []
        assert payload["next_action"] == ""

    def test_aggregate_payload_unchanged(self, awf_project: Path) -> None:
        """The pre-M1.2 fields are still populated from one call."""
        result = api.load_supervisor_context(awf_project)
        assert isinstance(result, api.SupervisorContextResult)
        assert result.project_name == "CtxTest"
        assert result.plan_md
        assert result.phase
        assert result.phase_prompt
        assert result.pipeline_running is False
        assert result.pipeline_pid is None
