"""SPEC A-run v1: autonomous run (забег) state, gates and evidence."""
from __future__ import annotations

from pathlib import Path

import pytest

import awf.api.pipeline as api_pipeline
from awf import api, run_state


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="RunTest")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = "TODO-0001", body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _fake_start(monkeypatch, proj: Path):
    captured: dict = {}

    def fake_bg(project_dir, **kw):
        captured.update(kw)
        return 424242, proj / ".agentic" / "logs" / "awf-start.out", None

    monkeypatch.setattr(api_pipeline, "start_in_background", fake_bg)
    monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda *a, **kw: True)
    return captured


class TestRunState:
    def test_roundtrip_merge_clear(self, tmp_git_repo):
        proj = tmp_git_repo
        assert run_state.read_run(proj) is None
        run_state.write_run(proj, active=True, queue=["TODO-0001"])
        run_state.write_run(proj, index=1)
        state = run_state.read_run(proj)
        assert state["active"] is True
        assert state["queue"] == ["TODO-0001"]
        assert state["index"] == 1
        run_state.clear_run(proj)
        assert run_state.read_run(proj) is None


class TestRunStart:
    def test_validates_queue(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        with pytest.raises(api.AwfApiError):
            api.run_start(proj, queue=[])
        with pytest.raises(api.AwfApiError):
            api.run_start(proj, queue=["nope"])
        with pytest.raises(api.AwfApiError):
            api.run_start(proj, queue=["TODO-0001", "TODO-0001"])

    def test_rejects_when_already_active(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        with pytest.raises(api.AwfApiError):
            api.run_start(proj, queue=["TODO-0002"])

    def test_writes_state_and_flags(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        result = api.run_start(
            proj,
            queue=["TODO-0001", "TODO-0002"],
            budget_minutes=90,
            stop_flags={"TODO-0002": ["phase-boundary"]},
        )
        assert result.position == "1/2"
        state = run_state.read_run(proj)
        assert state["stop_flags"] == {"TODO-0002": ["phase-boundary"]}
        assert state["budget_minutes"] == 90


class TestRunNextGates:
    def test_no_run_refused(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        result = api.run_next(proj)
        assert result.action == "refused"
        assert "No active run" in result.message

    def test_queue_exhausted_stops_with_report(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        run_state.write_run(proj, index=1)

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "queue exhausted" in result.message
        assert result.report_file
        assert Path(result.report_file).is_file()
        assert run_state.read_run(proj)["active"] is False

    def test_budget_exceeded_stops(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"], budget_minutes=1)
        run_state.write_run(proj, started_at="2020-01-01T00:00:00Z")

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "budget exhausted" in result.message

    def test_stop_flag_stops(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"], stop_flags={"TODO-0001": ["external-audit"]})

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "stop-list flag" in result.message
        assert "external-audit" in result.message

    def test_rejected_twice_stops(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        run_state.write_run(proj, rejects={"TODO-0001": 2})

        result = api.run_next(proj)

        assert result.action == "stopped"
        assert "rejected twice" in result.message

    def test_previous_not_finished_refuses(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])
        run_state.write_run(proj, index=1)  # TODO-0001 not archived

        result = api.run_next(proj)

        assert result.action == "refused"
        assert "TODO-0001 is not finished" in result.message

    def test_missing_todo_refuses(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        # TODO file not written yet

        result = api.run_next(proj)

        assert result.action == "refused"
        assert "missing or empty" in result.message
        assert "awf_run_next again" in result.next_action

    def test_happy_path_starts_next_item(self, tmp_git_repo, monkeypatch):
        proj = _project(tmp_git_repo)
        _write_todo(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])
        captured = _fake_start(monkeypatch, proj)

        result = api.run_next(proj)

        assert result.action == "started"
        assert result.todo_id == "TODO-0001"
        assert result.run_mode == "background"
        assert (proj / ".agentic" / "inbox" / "TODO-0001.ready").is_file()
        state = run_state.read_run(proj)
        assert state["index"] == 1
        assert state["current"] == "TODO-0001"


class TestEvidenceGate:
    def test_run_requires_evidence(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        with pytest.raises(api.AwfApiError, match="evidence"):
            api.approve_commit(proj, "TODO-0001")

    def test_evidence_recorded(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])

        result = api.approve_commit(
            proj, "TODO-0001",
            evidence="pytest -q → 348 passed; ruff → clean; verdict: approve",
        )

        assert result.evidence_file
        evidence = Path(result.evidence_file).read_text(encoding="utf-8")
        assert "348 passed" in evidence
        assert (proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").is_file()

    def test_no_run_evidence_optional(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        result = api.approve_commit(proj, "TODO-0001")
        assert result.evidence_file == ""
        assert (proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").is_file()


class TestRunFinish:
    def test_finish_writes_report(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])
        run_state.write_run(proj, completed=["TODO-0001"])

        result = api.run_finish(proj, reason="owner asked", summary="Stopped before C3.")

        assert result.active is False
        report = Path(result.report_file).read_text(encoding="utf-8")
        assert "owner asked" in report
        assert "TODO-0001" in report
        assert "Stopped before C3." in report
        assert run_state.read_run(proj)["active"] is False


class TestStatusIntegration:
    def test_status_carries_run_state(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])

        status = api.get_status(proj)

        assert status.run_state is not None
        assert status.run_state["active"] is True
        assert status.run_state["position"] == "1/2"


class TestRejectAccounting:
    """SPEC A-run.5: rejections are counted; the second stops the run."""

    def test_reject_counts_in_run(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])

        result = api.reject_commit(proj, "TODO-0001", "diff has a bug")

        assert result.rejects == 1
        assert result.run_stopped is False
        state = run_state.read_run(proj)
        assert state["rejects"] == {"TODO-0001": 1}
        assert state["outcomes"]["TODO-0001"]["verdict"] == "rejected"

    def test_second_reject_stops_run(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        api.reject_commit(proj, "TODO-0001", "still broken")

        result = api.reject_commit(proj, "TODO-0001", "broken again")

        assert result.run_stopped is True
        assert result.report_file
        assert run_state.read_run(proj)["active"] is False
        report = Path(result.report_file).read_text(encoding="utf-8")
        assert "rejected twice" in report
        assert "Run diary" in report

    def test_reject_outside_run_legacy(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        result = api.reject_commit(proj, "TODO-0001", "nope")
        assert result.rejects == 0
        assert Path(result.review_file).is_file()


class TestApproveDiary:
    def test_approve_records_outcome(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])

        api.approve_commit(proj, "TODO-0001", evidence="probes ok")

        assert run_state.read_run(proj)["outcomes"]["TODO-0001"]["verdict"] == "approved"


class TestDestructiveGate:
    """SPEC A-run.4: rollback(hard) is an owner decision during a run."""

    def test_hard_rollback_refused_during_run(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.create_baseline(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])

        with pytest.raises(api.AwfApiError, match="stop-list"):
            api.rollback(proj, "TODO-0001", mode="hard")

    def test_soft_rollback_allowed(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.create_baseline(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])

        api.rollback(proj, "TODO-0001", mode="soft")  # must not raise


class TestReportHealth:
    def test_report_counts_salvage_events(self, tmp_git_repo):
        from datetime import datetime, timezone

        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        (logs / "orchestrator.log").write_text(
            f"[{ts}Z] No signal after agent-x — salvage path\n"
            f"[{ts}Z] No signal after agent-y — salvage path\n",
            encoding="utf-8",
        )

        result = api.run_finish(proj, reason="test")

        report = Path(result.report_file).read_text(encoding="utf-8")
        assert "Salvage events:** 2" in report
