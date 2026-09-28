"""ORCH M4.2 (остаток ORCH-04): progress control + goal reconciliation.

1. Stalled-stage visibility — when the ACTIVE pipeline stage has not
   produced a state event (stage start, worker signal, checkpoint) for a
   configurable while, ``awf_brief`` and ``awf_run_status`` show a line
   that names the stage, the TODO and the quiet duration. The threshold
   is ``run.stall_minutes`` in ``.agentic/config.yaml`` — absent or 0
   means the warning is OFF (opt-in diagnostic, not a gate). The
   timestamps are the stock ones: ``current.yaml``'s ``updated_at``
   (written on every stage/signal/checkpoint transition) and the
   stage's ``Stage N:`` stamp in ``orchestrator.log`` — not a new store.

2. Goal reconciliation in the RUN-REPORT — a "Goal vs done" section:
   goal + criteria from the run state, done/remaining queue items,
   risks (salvage events, rejects, stop reason) and the proposed next
   step (next_action style). Empty goal/criteria → the section is
   skipped with a marker line, no error.

3. Degradation (M1 style): broken timestamps / broken plan fields →
   no warning or traceback, the surfaces keep working.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from awf import api, run_state

T = "TODO-0001"
T2 = "TODO-0002"
T3 = "TODO-0003"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="RunProgress")
    return tmp_git_repo


def _raw_write(proj: Path, state: dict) -> None:
    """Write run.yaml exactly as given — simulates an on-disk corruption
    the sanitized reader must survive (the normal write path cannot
    produce it)."""
    f = run_state.run_file(proj)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(state, allow_unicode=True), encoding="utf-8")


def _set_stall_minutes(proj: Path, minutes: object) -> None:
    cfg_file = proj / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    data.setdefault("run", {})["stall_minutes"] = minutes
    cfg_file.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _make_stalled_stage(
    proj: Path,
    minutes_ago: int,
    stage: str = "agent-implementer",
    todo: str = T,
) -> None:
    """A live stage with old timestamps — the stock markers of a stalled
    worker stage: current.yaml carrying the stage markers + an
    ``updated_at`` N minutes back, and the stage's ``Stage N:`` stamp in
    orchestrator.log at the same old moment."""
    old = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    state_dir = proj / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "current.yaml").write_text(
        yaml.safe_dump(
            {
                "stage_name": stage,
                "stage_kind": "execute",
                "stage_idx": 1,
                "todo_id": todo,
                "pipeline_pid": 424242,
                "updated_at": old.replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    log_dir = proj / ".agentic" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = old.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    (log_dir / "orchestrator.log").write_text(
        f"[{stamp}] Pipeline started (default)\n"
        f"[{stamp}] Stage 1: {stage} (worker :: execute)\n",
        encoding="utf-8",
    )


def _stall_line(card) -> str:
    lines = [ln for ln in card.text.splitlines() if "stall" in ln]
    assert lines, f"no stall line in the card:\n{card.text}"
    return lines[0]


class TestStalledStageBrief:
    def test_brief_warns_about_stalled_stage(self, tmp_git_repo: Path) -> None:
        """(invariant 1) the brief card names the stalled stage: the
        stage, the TODO, the quiet duration (and the run_status surface
        carries the same line). Red-before: no stall line at all."""
        proj = _project(tmp_git_repo)
        api.run_start(
            proj, queue=[T, T2], goal="Stabilize ORCH M4", criteria=["gates green"]
        )
        _set_stall_minutes(proj, 30)
        _make_stalled_stage(proj, 95)
        card = api.brief(proj)
        assert card.stall
        line = _stall_line(card)
        assert "agent-implementer" in line
        assert T in line
        assert "95 min" in line
        # the card keeps its word budget with the run + stall lines
        from awf.brief import MAX_WORDS

        assert len(card.text.split()) <= MAX_WORDS
        st = api.run_status(proj)
        assert st.stall
        assert "agent-implementer" in st.message
        assert "95 min" in st.message

    def test_stall_off_when_config_absent(self, tmp_git_repo: Path) -> None:
        """Absent ``run.stall_minutes`` → the warning is off (opt-in)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _make_stalled_stage(proj, 95)
        card = api.brief(proj)
        assert not card.stall
        assert "stall" not in card.text

    def test_stall_zero_disables(self, tmp_git_repo: Path) -> None:
        """Explicit ``run.stall_minutes: 0`` → the warning is off."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 0)
        _make_stalled_stage(proj, 95)
        card = api.brief(proj)
        assert not card.stall

    def test_stall_within_threshold_stays_quiet(self, tmp_git_repo: Path) -> None:
        """A young stage (under the threshold) gets no warning."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 30)
        _make_stalled_stage(proj, 10)
        card = api.brief(proj)
        assert not card.stall

    def test_stall_no_pipeline_state_is_quiet(self, tmp_git_repo: Path) -> None:
        """No current.yaml (pipeline not running) → nothing to measure."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 30)
        card = api.brief(proj)
        assert not card.stall

    def test_stall_broken_config_degrades_off(self, tmp_git_repo: Path) -> None:
        """A non-numeric ``run.stall_minutes`` degrades to OFF — no
        traceback (M1 style)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, "abc")
        _make_stalled_stage(proj, 95)
        card = api.brief(proj)
        assert not card.stall


class TestStalledStageDegradation:
    def test_broken_updated_at_falls_back_to_log_stamp(self, tmp_git_repo: Path) -> None:
        """A corrupt ``updated_at`` in current.yaml degrades to the
        stage's log stamp — the stall is still detected."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 30)
        _make_stalled_stage(proj, 95)
        f = proj / ".agentic" / "state" / "current.yaml"
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["updated_at"] = "not-a-timestamp"
        f.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        card = api.brief(proj)
        assert card.stall
        assert "95 min" in card.stall

    def test_missing_log_falls_back_to_state_stamp(self, tmp_git_repo: Path) -> None:
        """No orchestrator.log → the state's ``updated_at`` still works."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 30)
        _make_stalled_stage(proj, 95)
        (proj / ".agentic" / "logs" / "orchestrator.log").unlink()
        card = api.brief(proj)
        assert card.stall
        assert "95 min" in card.stall

    def test_no_timestamps_at_all_stays_quiet(self, tmp_git_repo: Path) -> None:
        """Neither a parseable ``updated_at`` nor a log stamp → there is
        nothing to measure: no warning, no traceback."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        _set_stall_minutes(proj, 30)
        _make_stalled_stage(proj, 95)
        (proj / ".agentic" / "logs" / "orchestrator.log").unlink()
        f = proj / ".agentic" / "state" / "current.yaml"
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["updated_at"] = "garbage"
        f.write_text(yaml.safe_dump(data, sort_keys=True), encoding="utf-8")
        card = api.brief(proj)
        assert not card.stall


class TestGoalVsDoneReport:
    def test_run_report_reconciles_goal(self, tmp_git_repo: Path) -> None:
        """(invariant 2) the RUN-REPORT reconciles the goal with what was
        done: goal + criteria, done/remaining queue items, risks, the
        proposed next step. Red-before: the section is absent."""
        proj = _project(tmp_git_repo)
        api.run_start(
            proj,
            queue=[T, T2, T3],
            goal="Ship M4.2",
            criteria=["gates green", "no second store"],
        )
        # T approved + credited, T2 in flight, T3 not started
        run_state.write_run(proj, completed=[T], index=1, current=T2)
        result = api.run_finish(proj, reason="test finish")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        assert "## Goal vs done" in text
        section = text.split("## Goal vs done", 1)[1]
        assert "Ship M4.2" in section
        assert "gates green" in section
        assert "1/3 done" in section
        assert T in section  # the completed item
        assert T2 in section and T3 in section  # the remaining items
        assert "Next step" in section
        assert "awf_run_next" in section

    def test_report_without_plan_gets_a_marker(self, tmp_git_repo: Path) -> None:
        """(invariant 2, degradation) empty goal/criteria → the section is
        skipped with a marker line, no error."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])
        result = api.run_finish(proj, reason="test finish")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        assert "## Goal vs done" in text
        section = text.split("## Goal vs done", 1)[1]
        assert "no goal or criteria" in section

    def test_report_broken_plan_fields_no_traceback(self, tmp_git_repo: Path) -> None:
        """(invariant 3) broken goal/criteria in run.yaml degrade to the
        marker — the report is still written, no traceback."""
        proj = _project(tmp_git_repo)
        _raw_write(
            proj,
            {
                "active": True,
                "queue": [T],
                "started_at": run_state.now_iso(),
                "goal": {"not": "a string"},
                "criteria": 42,
            },
        )
        result = api.run_finish(proj, reason="test finish")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        assert "## Goal vs done" in text
        assert "no goal or criteria" in text

    def test_report_risks_and_exhausted_queue(self, tmp_git_repo: Path) -> None:
        """The risk summary (salvage, rejects, stop) and the next step
        for an exhausted queue (a new run, not awf_run_next)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T, T2], goal="Ship M4.2", criteria=["gates green"])
        api.reject_commit(proj, T, "tests red: 3 failures")
        # a salvage event in the log, timestamped after the run started
        log_dir = proj / ".agentic" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        (log_dir / "orchestrator.log").write_text(
            f"[{stamp}] salvage path for {T}\n", encoding="utf-8"
        )
        run_state.write_run(proj, completed=[T, T2], index=2)
        result = api.run_finish(proj, reason="queue exhausted — all items processed")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        section = text.split("## Goal vs done", 1)[1]
        assert "salvage" in section
        assert "reject" in section
        assert "2/2 done" in section
        assert "awf_run_start" in section
        assert "awf_run_next" not in section

    def test_report_risks_stop_reason_is_the_run_stop_reason(
        self, tmp_git_repo: Path
    ) -> None:
        """(invariant 2) the Risks line's ``stop:`` is the RUN's stop
        reason — not the last decision's reason. A run with any recorded
        decision (approve/reject during the run) renders the decisions
        section before the goal check; the decisions loop in
        _write_report must not clobber the report's stop reason. Red:
        with one recorded reject, the risks line showed the reject
        reason as the stop (awf/api/run.py shadowed the function's
        ``reason`` parameter inside the decisions loop)."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T, T2], goal="Ship M4.2", criteria=["gates green"])
        api.reject_commit(proj, T, "tests red: 3 failures")
        run_state.write_run(proj, completed=[T, T2], index=2)
        result = api.run_finish(proj, reason="queue exhausted — all items processed")
        assert result.report_file
        text = Path(result.report_file).read_text(encoding="utf-8")
        section = text.split("## Goal vs done", 1)[1]
        assert "stop: queue exhausted — all items processed" in section
        assert "stop: tests red" not in section
