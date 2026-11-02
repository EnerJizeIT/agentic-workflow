"""TODO-0348 (IMP5): entry, situations, advice and the ≤5-tool row.

Every state S0–S13/S1a answers the four AC13 elements; the run-aware
branches (S11a/S11b/S12) survive only while the advice layer reaches them.
"""
from __future__ import annotations

import re

import pytest

from awf.brief import (
    ENTRY_FIELD,
    SITUATION_ADVICE,
    SITUATION_LINES,
    build_brief,
    classify_status,
    refine_with_run,
    situation_for,
)

from .brief_card_support import project, record, situations, status

# ─── Entry, situation, advice ────────────────────────────────────────────


class TestEntryAndSituations:
    def test_entry_constant(self):
        assert ENTRY_FIELD == {
            "tool": "awf_brief",
            "single": True,
            "recall_when": ["unclear moment", "lost context", "stage changed"],
        }

    @pytest.mark.parametrize("situation,state,run", situations())
    def test_each_state_has_words_and_advice(self, situation, state, run):
        code = situation_for(state, run)
        assert code == situation
        assert code in SITUATION_LINES
        assert not re.search(r"\bS\d", SITUATION_LINES[code])
        assert code in SITUATION_ADVICE and SITUATION_ADVICE[code]

    @pytest.mark.parametrize("situation,state,run", situations())
    def test_card_carries_entry_line_in_every_state(
        self, tmp_path, situation, state, run
    ):
        r = build_brief(project(tmp_path), status=state, run_record=run)
        assert r.entry == ENTRY_FIELD
        assert "single entry" in r.text
        assert f"- situation: {r.situation_line}" in r.text
        assert r.next_action.strip()
        assert r.advice_tool in SITUATION_ADVICE.values()

    def test_stale_stage_while_running_is_s5(self):
        state = status(
            stale_stage="STALE stage verify — dead",
            pipeline_running=True,
            expected_action="VERIFY STAGE — wait",
        )
        assert classify_status(state) == "S5"

    def test_checkpoint_is_weaker_than_salvage(self):
        state = status(
            checkpoint_pending=True, salvage_needed=True, salvage_stage="verify"
        )
        assert classify_status(state) == "S1"

    def test_foreign_commit_beats_salvage(self):
        state = status(
            foreign_commit="abc", salvage_needed=True, salvage_stage="verify"
        )
        assert classify_status(state) == "S1a"

    def test_reject_branches_by_run(self):
        reject = {"kind": "reject", "todo_id": "TODO-1"}
        assert refine_with_run("S10", status(), record(active=True, last_decision=reject)) == "S11a"
        assert refine_with_run("S10", status(), record(active=False, last_decision=reject)) == "S11b"

    def test_s0_does_not_claim_a_missing_agentic_dir(self, tmp_path):
        """AUD3: a project right after ``awf_init`` was told the dir is absent."""
        root = tmp_path / "fresh"
        (root / ".agentic").mkdir(parents=True)
        r = build_brief(root)
        assert r.situation == "S0"
        assert r.situation_line != SITUATION_LINES["S0"]
        assert "no .agentic/" not in r.text
        assert f"- tool: `{r.advice_tool}`" in r.text


# ─── Run-aware branches survive only while the advice reaches them ───────


class TestRunBranchGuards:
    """The card must not name a queue step that already ran.

    ``refine_with_run`` used to read ``last_decision`` alone. After
    ``awf_run_next`` the previous APPROVE is still the last decision on
    disk, so every later unit's card claimed "a unit is approved" and
    printed ``awf_run_next`` next to the real "worker stage running" advice.
    """

    APPROVE = {"kind": "approve", "todo_id": "TODO-0001"}
    REJECT = {"kind": "reject", "todo_id": "TODO-0001"}

    def test_approve_after_run_next_falls_back(self):
        run = record(
            active=True,
            current="TODO-0002",  # read_run_record's "next item" view
            last_decision=self.APPROVE,
            state={
                "active": True,
                "current": "TODO-0002",
                "queue": [{"todo_id": "TODO-0001"}, {"todo_id": "TODO-0002"}],
                "index": 1,
                "decisions": [self.APPROVE],
            },
        )
        state = status(
            pipeline_running=True,
            expected_action="worker stage running — auto-transition",
        )
        assert refine_with_run("S7", state, run) == "S7"

    def test_approve_before_run_next_uses_the_raw_current(self):
        run = record(
            active=True,
            current="TODO-0002",  # the display correction already moved on
            last_decision=self.APPROVE,
            state={
                "active": True,
                "current": "TODO-0001",  # raw state still names the approved
                "queue": [{"todo_id": "TODO-0001"}, {"todo_id": "TODO-0002"}],
                "index": 0,
                "decisions": [self.APPROVE],
            },
        )
        assert refine_with_run("S9", status(), run) == "S12"

    def test_approve_in_a_closed_run_is_history(self):
        run = record(active=False, last_decision=self.APPROVE)
        assert refine_with_run("S9", status(), run) == "S9"

    def test_reject_closed_by_revise_falls_back(self):
        run = record(
            active=True,
            last_decision=self.REJECT,
            state={
                "active": True,
                "current": "TODO-0009",
                "queue": [{"todo_id": "TODO-0009"}],
                "index": 0,
                "decisions": [self.REJECT],
            },
        )
        assert refine_with_run("S7", status(), run) == "S7"

    def test_card_situation_and_tool_agree_after_run_next(self, tmp_path):
        run = record(
            active=True,
            last_decision=self.APPROVE,
            state={
                "active": True,
                "current": "TODO-0002",
                "queue": [{"todo_id": "TODO-0001"}, {"todo_id": "TODO-0002"}],
                "index": 1,
                "decisions": [self.APPROVE],
            },
        )
        state = status(
            pipeline_running=True,
            current_stage_name="worker",
            current_stage_kind="execute",
            expected_action="worker stage running — auto-transition",
        )
        r = build_brief(project(tmp_path), status=state, run_record=run)
        assert r.situation == "S7"
        assert r.advice_tool == "awf_wait_for_event"
        assert "approved" not in r.situation_line
        assert "- tool: `awf_run_next`" not in r.text


# ─── Tools: ≤5, advice first ─────────────────────────────────────────────


class TestSituationalTools:
    @pytest.mark.parametrize("situation,state,run", situations())
    def test_rows_are_small_and_advice_first(self, tmp_path, situation, state, run):
        r = build_brief(project(tmp_path), status=state, run_record=run)
        assert len(r.tools) <= 5
        assert r.tools
        assert r.tools[0]["tool"] == r.advice_tool
        assert all(entry["why"].strip() for entry in r.tools)
