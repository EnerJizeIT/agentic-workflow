"""NEG-5 (day-2 B2): escalation signals survive process time.

Incident: a BLOCKED escalation instantly "completed" its replan wait with
the very TODO being retried (stale .ready, picked up by the plan-kind orphan
logic), then ``_find_active_todo`` saw the BLOCKED-closed TODO → None → the
process stopped. The supervisor's later ACK had nobody to read it.
"""
from __future__ import annotations

import os
import time as _time

import pytest

import awf.pipeline_engine as engine
from awf.pipeline import Stage
from awf.supervisor import wait_for_supervisor_signal


def _project(tmp_path):
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    return proj


def _todo(proj, todo_id="TODO-0009", mtime: float | None = None):
    ready = proj / ".agentic" / "inbox" / f"{todo_id}.ready"
    ready.write_text("", encoding="utf-8")
    (proj / ".agentic" / "inbox" / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")
    if mtime is not None:
        os.utime(ready, (mtime, mtime))
    return ready


def _blocked(proj, todo_id="TODO-0009"):
    outbox = proj / ".agentic" / "outbox"
    (outbox / f"BLOCKED-{todo_id}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{todo_id}.md").write_text("# Blocked\n", encoding="utf-8")


class TestReplanWait:
    def test_stale_todo_does_not_satisfy_wait(self, tmp_path, monkeypatch):
        """The pre-escalation TODO-0009.ready must NOT complete the replan wait."""
        proj = _project(tmp_path)
        _todo(proj, mtime=_time.time() - 1000)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "replan", "TODO-0009", proj, proj / ".agentic" / "logs", timeout=1,
            )

    def test_fresh_todo_satisfies_wait(self, tmp_path, monkeypatch):
        """A TODO created AFTER the escalation is the supervisor's answer."""
        proj = _project(tmp_path)
        _todo(proj, mtime=_time.time() + 0.5)  # created after wait start
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        sig = wait_for_supervisor_signal(
            "replan", "TODO-0009", proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == "TODO-0009"

    def test_ack_satisfies_wait(self, tmp_path, monkeypatch):
        """ACK for the current TODO = "accept and continue"."""
        proj = _project(tmp_path)
        _todo(proj, mtime=_time.time() - 1000)
        (proj / ".agentic" / "inbox" / "ACK-TODO-0009.ready").write_text("", encoding="utf-8")
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        sig = wait_for_supervisor_signal(
            "replan", "TODO-0009", proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == "ACK-TODO-0009"


class TestEscalateOnBlocked:
    def _stage(self):
        return Stage(name="agent-qa-review-final", role="agent-qa-review", kind="execute")

    def test_ack_unblocks_todo_and_retries_same_stage(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)
        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "ACK-TODO-0009")

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review-final", "TODO-0009",
            auto=False, stage=self._stage(), retry_counts=[0, 0, 0, 0], stage_idx=3,
            pipeline_name=None,
        )

        assert result == (3, "TODO-0009", 0), "ACK → retry the same stage"
        outbox = proj / ".agentic" / "outbox"
        assert not (outbox / "BLOCKED-TODO-0009.ready").exists()
        assert (proj / ".agentic" / "context" / "BLOCKED-TODO-0009.ready").exists()
        assert (proj / ".agentic" / "context" / "BLOCKED-TODO-0009.md").exists()

        from awf import todos
        assert todos.newest_active(proj) == "TODO-0009", "TODO active again"

    def test_no_answer_stops_with_clear_path(self, tmp_path, monkeypatch):
        """No TODO and no ACK → stop (but the wait no longer self-satisfies)."""
        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)
        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "")

        result = engine._handle_escalate(
            proj, proj / ".agentic" / "logs", "agent-qa-review-final", "TODO-0009",
            auto=False, stage=self._stage(), retry_counts=[0, 0, 0, 0], stage_idx=3,
            pipeline_name=None,
        )

        assert result == (3, "TODO-0009", 1)


class TestContinueWithBlocked:
    """Day-2 B3: `awf continue` must not dead-end on blocked TODOs."""

    def _fake_bg(self, monkeypatch, proj):
        import awf.api.pipeline as api_pipeline

        captured: dict = {}

        def fake_bg(project_dir, **kw):
            captured.update(kw)
            return 12345, proj / ".agentic" / "logs" / "awf-start.out", None

        monkeypatch.setattr(api_pipeline, "start_in_background", fake_bg)
        monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda *a, **kw: True)
        return captured

    def test_pending_ack_resumes_blocked_todo(self, tmp_path, monkeypatch):
        """ACK written after the process died → consumed, BLOCKED cleared, resume."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)
        (proj / ".agentic" / "inbox" / "ACK-TODO-0009.ready").write_text("", encoding="utf-8")

        from awf.pipeline_state import write_state
        write_state(proj, stage_name="agent-implementer", stage_kind="execute", todo_id="TODO-0009")

        captured = self._fake_bg(monkeypatch, proj)
        result = api_pipeline.continue_pipeline(proj, background=True)

        assert result.run_mode == "background"
        assert "consumed ACK-TODO-0009.ready" in result.message
        assert "BLOCKED cleared" in result.message
        assert captured["from_stage"] == "agent-implementer"
        assert not (proj / ".agentic" / "inbox" / "ACK-TODO-0009.ready").exists()
        assert (proj / ".agentic" / "context" / "BLOCKED-TODO-0009.ready").exists()

    def test_blocked_without_answer_instructs_instead_of_dead_end(self, tmp_path):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)

        result = api_pipeline.continue_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert "BLOCKED" in result.message
        assert "--ack TODO-0009" in result.message

    def test_ack_flag_resolves_and_resumes(self, tmp_path, monkeypatch):
        """`awf continue --ack TODO-0009` — no manual touch needed."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)
        from awf.pipeline_state import write_state
        write_state(proj, stage_name="agent-qa-review-final", stage_kind="execute", todo_id="TODO-0009")

        captured = self._fake_bg(monkeypatch, proj)
        result = api_pipeline.continue_pipeline(proj, background=True, ack="TODO-0009")

        assert result.run_mode == "background"
        assert captured["from_stage"] == "agent-qa-review-final"
        assert not (proj / ".agentic" / "inbox" / "ACK-TODO-0009.ready").exists()
        assert not (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0009.ready").exists()

    def test_invalid_ack_id_rejected(self, tmp_path):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        result = api_pipeline.continue_pipeline(proj, background=False, ack="not-a-todo")

        assert result.run_mode == "noop"
        assert "Invalid TODO id" in result.message

    def test_approve_for_active_todo_is_not_consumed(self, tmp_path, monkeypatch):
        """BD-8 regression: a live TODO's APPROVE belongs to the commit gate."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _todo(proj)  # active — no closure
        (proj / ".agentic" / "inbox" / "APPROVE-TODO-0009.ready").write_text("", encoding="utf-8")

        self._fake_bg(monkeypatch, proj)
        result = api_pipeline.continue_pipeline(proj, background=True)

        assert result.run_mode == "background"
        assert (proj / ".agentic" / "inbox" / "APPROVE-TODO-0009.ready").exists()

    def test_pending_review_is_reported(self, tmp_path):
        """A REVIEW left behind must be visible in the answer, not a silent stop."""
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_path)
        _todo(proj)
        _blocked(proj)
        (proj / ".agentic" / "outbox" / "REVIEW-TODO-0009.md").write_text(
            "rejected: redo X\n", encoding="utf-8",
        )

        result = api_pipeline.continue_pipeline(proj, background=False)

        assert result.run_mode == "noop"
        assert "REVIEW for TODO-0009" in result.message
