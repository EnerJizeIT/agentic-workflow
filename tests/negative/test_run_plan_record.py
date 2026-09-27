"""ORCH M1.1: RunPlan — goal, criteria and causal decisions in run state.

The run state (`.agentic/state/run.yaml`) is the single source; the RunPlan
is DERIVED from it (owner decision, IMPLEMENTATION-STRATEGY §1.1): no
second store. goal/criteria are written by run_start; decisions is an
append-only causal memory fed by approve/reject inside an active run;
old state files read without migration; broken fields degrade to empty
with a warning, never a traceback.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from awf import api, git_utils, run_state

T = "TODO-0001"
T2 = "TODO-0002"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="RunPlan")
    return tmp_git_repo


def _raw_write(proj: Path, state: dict) -> None:
    """Write run.yaml exactly as given — simulates an on-disk corruption the
    sanitized reader must survive (the normal write path cannot produce it)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")


def test_run_start_records_goal_and_criteria(tmp_git_repo: Path) -> None:
    """(a) goal + criteria are stored in the run state and read back."""
    proj = _project(tmp_git_repo)
    api.run_start(
        proj,
        queue=[T],
        goal="Stabilize the run state",
        criteria=["gates green", "no second store"],
    )
    state = run_state.read_run(proj)
    assert state["goal"] == "Stabilize the run state"
    assert state["criteria"] == ["gates green", "no second store"]

    st = api.run_status(proj)
    assert st.goal == "Stabilize the run state"
    assert st.criteria == ["gates green", "no second store"]


def test_reject_reason_lands_in_decisions(tmp_git_repo: Path) -> None:
    """(b) a reject inside an active run appends {ts, kind, todo_id, reason}."""
    proj = _project(tmp_git_repo)
    api.run_start(proj, queue=[T])
    api.reject_commit(proj, T, "tests red: 3 failures in test_run.py")
    state = run_state.read_run(proj)
    assert state["rejects"] == {T: 1}
    entry = state["decisions"][-1]
    assert entry["kind"] == "reject"
    assert entry["todo_id"] == T
    assert entry["reason"] == "tests red: 3 failures in test_run.py"
    assert entry["ts"]


class TestRunStartPlanFields:
    def test_without_plan_fields_defaults_to_empty(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        state = run_state.read_run(proj)
        assert state["goal"] == ""
        assert state["criteria"] == []
        assert state["decisions"] == []

    def test_legacy_run_yaml_without_new_fields(self, tmp_git_repo: Path) -> None:
        """A pre-M1.1 run.yaml (no goal/criteria/decisions keys) reads exactly
        as before — no migration (invariant 1)."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [T],
                "index": 0,
                "started_at": run_state.now_iso(),
            },
        )
        state = run_state.read_run(proj)
        assert state["active"] is True
        assert state["goal"] == ""
        assert state["criteria"] == []
        assert state["decisions"] == []
        st = api.run_status(proj)
        assert st.active is True
        assert st.goal == ""
        assert st.criteria == []
        assert st.decisions == []

    def test_new_run_resets_previous_plan_and_decisions(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T], goal="old goal", criteria=["old"])
        api.reject_commit(proj, T, "old reason")
        api.run_start(proj, queue=[T2], force=True)
        state = run_state.read_run(proj)
        assert state["goal"] == ""
        assert state["criteria"] == []
        assert state["decisions"] == []


class TestRejectDecisions:
    def test_reject_outside_run_writes_nothing(self, tmp_git_repo: Path) -> None:
        """Outside a run there is no run state — no decision, no file
        created (invariant 2)."""
        proj = _project(tmp_git_repo)
        api.reject_commit(proj, T, "legacy reject")
        assert run_state.read_run(proj) is None

    def test_reject_idempotent_same_reason(self, tmp_git_repo: Path) -> None:
        """(d) repeating the same (kind, todo_id, reason) adds no duplicate;
        the rejects counter (the twice-rejected gate) is unaffected."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        api.reject_commit(proj, T, "same reason")
        api.reject_commit(proj, T, "same reason")
        state = run_state.read_run(proj)
        rejects = [d for d in state["decisions"] if d["kind"] == "reject"]
        assert len(rejects) == 1
        assert state["rejects"] == {T: 2}

    def test_reject_different_reason_appends(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        api.reject_commit(proj, T, "reason one")
        api.reject_commit(proj, T, "reason two")
        state = run_state.read_run(proj)
        rejects = [d for d in state["decisions"] if d["kind"] == "reject"]
        assert [d["reason"] for d in rejects] == ["reason one", "reason two"]


class TestApproveDecisions:
    def test_approve_excerpt_lands_in_decisions(self, tmp_git_repo: Path) -> None:
        """(c) an approve inside a run appends a kind=approve entry with a
        short evidence excerpt (the full text stays in the RUN-EVIDENCE
        file)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        evidence = (
            "pytest -q → 348 passed; ruff → clean; diff checked; "
            "verdict: approve. "
        ) * 10  # ~430 chars — well over the 200 excerpt
        fp = git_utils.tree_fingerprint(proj)
        api.approve_commit(proj, T, evidence=evidence, verified_sha=fp)
        state = run_state.read_run(proj)
        entry = state["decisions"][-1]
        assert entry["kind"] == "approve"
        assert entry["todo_id"] == T
        assert entry["reason"] == evidence.strip()[:200]
        assert len(entry["reason"]) <= 200
        ev = proj / ".agentic" / "context" / f"RUN-EVIDENCE-{T}.md"
        assert evidence.strip() in ev.read_text(encoding="utf-8")

    def test_approve_idempotent_same_evidence(self, tmp_git_repo: Path) -> None:
        """(d) the same approve twice (idempotent signal) — one decision."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        evidence = "pytest -q → 348 passed; verdict: approve"
        api.approve_commit(
            proj, T, evidence=evidence, verified_sha=git_utils.tree_fingerprint(proj)
        )
        # the first approve wrote context files (untracked) — the tree
        # moved, so the second approve must pin the fresh fingerprint
        api.approve_commit(
            proj, T, evidence=evidence, verified_sha=git_utils.tree_fingerprint(proj)
        )
        state = run_state.read_run(proj)
        approves = [d for d in state["decisions"] if d["kind"] == "approve"]
        assert len(approves) == 1
        assert approves[0]["reason"] == evidence

    def test_refused_approve_after_reject_records_nothing(self, tmp_git_repo: Path) -> None:
        """A conflict (reject won) refuses the approve — no approve entry
        lands in the causal memory."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        api.reject_commit(proj, T, "diff too wide")
        evidence = "pytest -q → 348 passed; verdict: approve"
        result = api.approve_commit(
            proj, T, evidence=evidence, verified_sha=git_utils.tree_fingerprint(proj)
        )
        assert result.signal_file == ""  # conflict refusal
        state = run_state.read_run(proj)
        assert [d["kind"] for d in state["decisions"]] == ["reject"]


class TestRecordDecision:
    """The atomic standalone wrapper (one lock hold, active run only)."""

    def test_append_dedup_and_order(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T, T2])
        _, added = run_state.record_decision(proj, "reject", T, "r1")
        assert added is True
        _, added = run_state.record_decision(proj, "reject", T, "r1")
        assert added is False
        _, added = run_state.record_decision(proj, "reject", T, "r2")
        assert added is True
        state = run_state.read_run(proj)
        assert [d["reason"] for d in state["decisions"]] == ["r1", "r2"]

    def test_no_run_state_is_noop(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        state, added = run_state.record_decision(proj, "reject", T, "no run")
        assert state is None
        assert added is False
        assert run_state.read_run(proj) is None

    def test_inactive_run_is_noop(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        run_state.write_run(proj, active=False, queue=[T])
        state, added = run_state.record_decision(proj, "reject", T, "closed run")
        assert added is False
        assert state["decisions"] == []


class TestCorruptPlanFields:
    """(e) broken/partial goal/criteria/decisions in run.yaml degrade to
    empty with a warning — the rest of the run stays usable, no traceback
    (invariant 4)."""

    def test_broken_goal_degrades_to_empty(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": [T], "goal": {"not": "a string"}})
        state = run_state.read_run(proj)
        assert state is not None
        assert state["active"] is True
        assert state["goal"] == ""

    def test_broken_criteria_degrades_to_empty(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": [T], "criteria": "not-a-list"})
        state = run_state.read_run(proj)
        assert state["criteria"] == []

    def test_partial_criteria_keeps_valid_entries(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": [T], "criteria": ["ok", "", 7]})
        state = run_state.read_run(proj)
        assert state["criteria"] == ["ok", "7"]

    def test_garbage_decisions_value_degrades_to_list(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(proj, {"active": True, "queue": [T], "decisions": "corrupt"})
        state = run_state.read_run(proj)
        assert state["decisions"] == []

    def test_broken_decisions_degrade_entrywise(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [T],
                "decisions": [
                    "garbage",
                    {"kind": "reject", "todo_id": T, "reason": "real"},
                    {"no_kind": True},
                ],
            },
        )
        state = run_state.read_run(proj)
        assert state["decisions"] == [
            {"ts": "", "kind": "reject", "todo_id": T, "reason": "real"},
        ]

    def test_run_status_on_broken_fields(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [T],
                "goal": [1, 2],
                "criteria": 42,
                "decisions": {"x": 1},
            },
        )
        st = api.run_status(proj)
        assert st.active is True
        assert st.goal == ""
        assert st.criteria == []
        assert st.decisions == []


class TestRunReportPlan:
    """(3) the RUN-REPORT from run_finish shows the plan and the decisions."""

    def test_report_shows_goal_criteria_and_decisions(self, tmp_git_repo: Path) -> None:
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T], goal="Ship M1.1", criteria=["gates green"])
        api.reject_commit(proj, T, "diff too wide")
        result = api.run_finish(proj, reason="test finish")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        assert "Ship M1.1" in text
        assert "gates green" in text
        assert "diff too wide" in text
        assert "reject" in text
