"""Unit tests for the run record as the supervisor sees it.

ORCH M1.2: the run record fields (goal, criteria, position, budget,
decisions, sources, warning) and the run-aware next_action. The one-shot
``load_supervisor_context`` aggregate is gone (IMP1): the full view is
``awf.run_plan_read.read_run_record`` and the card is ``awf.brief``. The
two-views-of-one-record invariants on synthetic projects stay pinned in
the negative layer (tests/negative/test_context_single_source.py); here
the plain unit behavior.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import api, run_state
from awf.run_plan_read import decision_line, read_run_record

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
        result = read_run_record(awf_project)
        assert result.active is False
        assert result.goal == ""
        assert result.criteria == []
        assert result.position == "0/0"
        assert result.budget_minutes == 0
        assert result.budget_left_minutes == 0
        assert result.note == ""
        assert result.last_decision is None
        assert result.decisions == []
        assert result.sources == []
        assert result.warning == ""

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
        result = read_run_record(awf_project)
        assert result.active is True
        assert result.goal == "Stabilize the run state"
        assert result.criteria == ["gates green", "no second store"]
        assert result.position == "1/1"
        assert result.budget_minutes == 120
        # int truncation of the productive-elapsed minutes: a fresh run
        # shows 119..120 (same formula as run_brief)
        assert result.budget_left_minutes in (119, 120)
        assert ".agentic/state/run.yaml" in result.sources

    def test_decisions_carried_in_full(self, awf_project: Path) -> None:
        """The full view keeps every decision entry (which, why, when)."""
        api.run_start(awf_project, queue=[T])
        api.reject_commit(awf_project, T, "tests red: 3 failures")
        result = read_run_record(awf_project)
        assert len(result.decisions) == 1
        d = result.decisions[0]
        assert d["kind"] == "reject"
        assert d["todo_id"] == T
        assert d["reason"] == "tests red: 3 failures"
        assert d["ts"]
        assert decision_line(result.last_decision) == (
            f"reject {T} — tests red: 3 failures"
        )
        assert f"outbox/REVIEW-{T}.md" in result.sources

    def test_next_action_run_aware_after_reject(self, awf_project: Path) -> None:
        """After a reject in a run the nearest permitted action is the run
        one (awf_run_next), not the generic awf_start."""
        api.run_start(awf_project, queue=[T])
        api.reject_commit(awf_project, T, "diff too wide")
        result = api.brief(awf_project)
        assert "awf_run_next" in result.next_action
        assert "awf_start" not in result.next_action

    def test_next_action_run_aware_before_any_decision(
        self, awf_project: Path
    ) -> None:
        """An active run without decisions still leads to the next step."""
        api.run_start(awf_project, queue=[T], goal="no decisions yet")
        result = api.brief(awf_project)
        assert result.next_action

    def test_broken_run_yaml_degrades_with_warning(self, awf_project: Path) -> None:
        """A corrupted run.yaml degrades to "no run" with a warning in the
        record — no traceback (invariant 5)."""
        f = run_state.run_file(awf_project)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("active: [unclosed", encoding="utf-8")
        result = read_run_record(awf_project)
        assert result.warning
        assert "run.yaml" in result.warning
        assert result.active is False
        assert result.goal == ""
        assert result.decisions == []

    def test_brief_and_record_share_the_record(self, awf_project: Path) -> None:
        """The same reading on both surfaces: the card's compact fields
        equal the full record's (clipping aside)."""
        api.run_start(awf_project, queue=[T], goal="One record, two views")
        api.reject_commit(awf_project, T, "missing test")
        b = api.brief(awf_project)
        c = read_run_record(awf_project)
        assert b.run_goal == c.goal
        assert b.run_last_decision == decision_line(c.last_decision)
        assert b.run_sources == c.sources
        assert b.run_warning == c.warning


class TestPayloadShape:
    def test_as_dict_serializable_with_run_fields(self, awf_project: Path) -> None:
        """The run fields survive the JSON roundtrip (MCP response)."""
        api.run_start(awf_project, queue=[T], goal="json roundtrip")
        result = api.brief(awf_project)
        payload = json.loads(json.dumps(result.as_dict(), ensure_ascii=False))
        assert payload["run_goal"] == "json roundtrip"
        assert payload["run_sources"]
        assert "text" in payload

    def test_card_fields_populated_from_one_call(self, awf_project: Path) -> None:
        """The card is the single entry point: its fields come from one call."""
        result = api.brief(awf_project)
        assert result.project
        assert result.phase
        assert result.pipeline_running is False
        assert result.pipeline_pid is None
        assert result.text


class TestWorkerRejectionIsNotAVerifyDecision:
    """A32: a refusal writes its findings into the SAME unit.

    The supervisor bootstrap keyed off any ``REVIEW-`` signal, so a stopped
    run whose last signal was the worker's ``REVIEW-REJECTED-<id>`` was told
    to "write the next TODO" — the clone the engine no longer creates. Only
    the verify stage's own ``REVIEW-<id>`` decision may give that advice.
    """

    def _advice(self, last_signal: str) -> str:
        from awf.api.context import _compute_expected_action

        return _compute_expected_action(
            pipeline_running=False,
            current_stage_kind=None,
            checkpoint_pending=False,
            has_active_todos=True,
            done_count=0,
            last_signal=last_signal,
        ) or ""

    def test_the_verify_decision_still_asks_for_the_next_todo(self) -> None:
        assert "Write the next TODO" in self._advice("REVIEW-TODO-0001")

    def test_a_worker_rejection_keeps_the_owner_on_the_same_unit(self) -> None:
        advice = self._advice("REVIEW-REJECTED-TODO-0001")
        assert "Write the next TODO" not in advice
        assert "awf_start" in advice
