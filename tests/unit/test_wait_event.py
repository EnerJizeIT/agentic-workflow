"""DASH Phase 3: wait_for_event — supervisor wake-up tests."""
from __future__ import annotations

import pytest

from awf import api
from awf.pipeline_state import write_state


@pytest.fixture
def awf_project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


class TestWaitForEvent:
    """wait_for_event blocks until pipeline event or timeout."""

    def test_idle_when_no_state(self, awf_project):
        """No pipeline state file → returns 'idle' immediately."""
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "idle"
        assert "not running" in result.message

    def test_verify_event_returns_immediately(self, awf_project):
        """When state has stage_kind='verify' → returns 'verify' immediately."""
        write_state(
            awf_project,
            stage_name="verify",
            stage_kind="verify",
            stage_idx=5,
            todo_id="TODO-0001",
        )
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "verify"
        assert "VERIFY" in result.message
        assert result.state_snapshot["stage_kind"] == "verify"

    def test_blocked_event_reads_last_signal_from_state(self, awf_project):
        """Reader contract: a last_signal that starts with BLOCKED → 'blocked'.

        The key is written by the ENGINE (pipeline_engine.execute_agent_stage)
        — the end-to-end test below pins that; this test only checks that the
        reader branch works when the key is present.
        """
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
            last_signal="BLOCKED-TODO-0001",
        )
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "blocked"
        assert "BLOCKED-TODO-0001" in result.message

    def test_blocked_worker_end_to_end(self, awf_project, monkeypatch):
        """AUD02-01 end-to-end: BLOCKED worker → the ENGINE writes last_signal
        into state (the test does not write it) → wait_for_event returns
        'blocked' within one cycle."""
        import awf.pipeline_engine as engine
        from awf.pipeline import Stage
        from awf.pipeline_state import read_state

        outbox = awf_project / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        def fake_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            (outbox / f"BLOCKED-{todo_id}.md").write_text("cannot proceed\n", encoding="utf-8")
            (outbox / f"BLOCKED-{todo_id}.ready").write_text("", encoding="utf-8")

        monkeypatch.setattr(engine, "_run_agent_stage", fake_agent)
        monkeypatch.setattr(engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "_read_baseline_sha", lambda *a, **kw: "")
        monkeypatch.setattr(engine, "wait_for_signal", lambda *a, **kw: None)

        stage = Stage(name="impl", role="developer", kind="execute", on_blocked="stop")
        engine.execute_agent_stage(
            stage, "TODO-0001", awf_project, {}, awf_project / ".agentic" / "logs",
            [stage], 0, [0], True, None,
        )

        state = read_state(awf_project)
        assert state is not None
        assert state.get("last_signal") == "BLOCKED-TODO-0001", (
            "the engine must populate last_signal itself — the wake-up reads it from state"
        )

        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "blocked"

    def test_checkpoint_event_returns_immediately(self, awf_project):
        """When checkpoint_pending=True → returns 'checkpoint'."""
        write_state(
            awf_project,
            stage_name="plan",
            stage_kind="plan",
            checkpoint_pending=True,
            checkpoint_form_url="file:///tmp/form.html",
        )
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "checkpoint"
        assert "file:///tmp/form.html" in result.message

    def test_timeout_when_no_event(self, awf_project):
        """Pipeline running (execute stage) but no event → returns 'timeout'."""
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
            stage_idx=1,
            todo_id="TODO-0001",
        )
        result = api.wait_for_event(awf_project, timeout=2, poll_interval=1)
        assert result.event_type == "timeout"
        assert "2s" in result.message
        assert result.state_snapshot["stage_name"] == "developer"

    def test_done_when_state_cleared(self, awf_project, monkeypatch):
        """State file removed during wait → returns 'done'."""
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
        )

        # Simulate state being cleared after first poll
        call_count = {"n": 0}
        original_read = api.wait_for_event.__globals__["read_state"]

        def flaky_read_state(project_dir):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                return None  # state cleared
            return original_read(project_dir)

        monkeypatch.setattr(
            "awf.api.wait_event.read_state", flaky_read_state
        )

        result = api.wait_for_event(awf_project, timeout=5, poll_interval=1)
        assert result.event_type == "done"
        assert "Pipeline exited" in result.message

    def test_execute_stage_does_not_trigger_immediately(self, awf_project):
        """Execute stage without BLOCKED/checkpoint → no immediate event."""
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
        )
        # Should timeout (not return immediately with execute)
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)
        assert result.event_type == "timeout"

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.wait_for_event(tmp_git_repo, timeout=1)

    def test_as_dict_serializable(self, awf_project):
        """Result.as_dict() JSON-serializable for MCP."""
        import json

        write_state(awf_project, stage_kind="verify", stage_name="verify")
        result = api.wait_for_event(awf_project, timeout=1)
        d = result.as_dict()
        json.dumps(d)

    def test_state_snapshot_has_relevant_fields(self, awf_project):
        """state_snapshot includes fields supervisor needs."""
        write_state(
            awf_project,
            stage_kind="verify",
            stage_name="verify",
            stage_idx=3,
            todo_id="TODO-0001",
            last_signal="DONE-TODO-0001",
        )
        result = api.wait_for_event(awf_project, timeout=1)
        snap = result.state_snapshot
        assert snap["stage_name"] == "verify"
        assert snap["stage_kind"] == "verify"
        assert snap["todo_id"] == "TODO-0001"
        assert snap["last_signal"] == "DONE-TODO-0001"


class TestSuggestedTimeout:
    """SPEC A-run: the wait size is suggested from measured stage durations."""

    def test_suggestion_from_stage_history(self, awf_project):
        from datetime import datetime, timedelta

        from awf.api.wait_event import TRANSPORT_CAP, _suggest_timeout

        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=12 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        suggested = _suggest_timeout(awf_project)

        # 12 min stage → raw 240s — clamped to the MCP transport cap (B3):
        # a suggested wait above the cap would die mid-wait (-32001).
        assert suggested == TRANSPORT_CAP
        assert suggested <= TRANSPORT_CAP

    def test_suggestion_short_stage_under_cap(self, awf_project):
        from datetime import datetime, timedelta

        from awf.api.wait_event import MIN_WAIT, TRANSPORT_CAP, _suggest_timeout

        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=2 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        suggested = _suggest_timeout(awf_project)

        # 2 min stage → raw 40s — below the cap, kept as-is.
        assert suggested == 40
        assert MIN_WAIT <= suggested <= TRANSPORT_CAP

    def test_suggestion_default_without_log(self, awf_project):
        from awf.api.wait_event import TRANSPORT_CAP, _suggest_timeout

        assert _suggest_timeout(awf_project) == TRANSPORT_CAP
        # A stale caller default (180) must not escape the cap either.
        assert _suggest_timeout(awf_project, default=180) == TRANSPORT_CAP

    def test_timeout_event_carries_suggestion(self, awf_project):
        from awf.api.wait_event import MIN_WAIT, TRANSPORT_CAP

        write_state(
            awf_project, stage_name="agent-impl", stage_kind="execute", stage_idx=2,
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)
        assert result.event_type == "timeout"
        assert MIN_WAIT <= result.suggested_timeout <= TRANSPORT_CAP

    def test_clamped_suggestion_advises_smaller_steps(self, awf_project):
        """B3: the suggestion hit the transport cap → the message advises
        waiting in smaller steps instead of one long cut-off wait."""
        from datetime import datetime, timedelta

        from awf.api.wait_event import TRANSPORT_CAP

        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=12 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        write_state(awf_project, stage_name="agent-impl", stage_kind="execute", stage_idx=2)
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)

        assert result.event_type == "timeout"
        assert result.suggested_timeout == TRANSPORT_CAP
        assert "Transport cap" in result.message
        assert "smaller steps" in result.message


class TestActionableOnly:
    """actionable_only suppresses stage_changed noise in the run loop."""

    def test_stage_changed_suppressed(self, awf_project, monkeypatch):
        import threading
        import time as _time

        write_state(awf_project, stage_name="stage-a", stage_kind="execute", stage_idx=1)

        def flip_later():
            _time.sleep(0.4)
            write_state(awf_project, stage_name="stage-b", stage_kind="execute", stage_idx=2)

        threading.Thread(target=flip_later, daemon=True).start()

        result = api.wait_for_event(
            awf_project, timeout=2, poll_interval=1, actionable_only=True,
        )

        # The transition happened but must not wake the caller.
        assert result.event_type == "timeout"

    def test_stage_changed_returned_by_default(self, awf_project):
        import threading
        import time as _time

        write_state(awf_project, stage_name="stage-a", stage_kind="execute", stage_idx=1)

        def flip_later():
            _time.sleep(0.4)
            write_state(awf_project, stage_name="stage-b", stage_kind="execute", stage_idx=2)

        threading.Thread(target=flip_later, daemon=True).start()

        result = api.wait_for_event(awf_project, timeout=3, poll_interval=1)

        from awf.api.wait_event import MIN_WAIT, TRANSPORT_CAP

        assert result.event_type == "stage_changed"
        assert MIN_WAIT <= result.suggested_timeout <= TRANSPORT_CAP


class TestVerifyPayload:
    def test_verify_snapshot_carries_diff_stat(self, awf_project):
        import subprocess

        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=awf_project,
            capture_output=True, text=True,
        ).stdout.strip()
        ctx = awf_project / ".agentic" / "context"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "BASELINE-TODO-0001.sha").write_text(sha + "\n", encoding="utf-8")
        (awf_project / "README.md").write_text("changed by the stage\n", encoding="utf-8")
        write_state(
            awf_project, stage_name="verify", stage_kind="verify",
            stage_idx=5, todo_id="TODO-0001",
        )

        result = api.wait_for_event(awf_project, timeout=1)

        assert result.event_type == "verify"
        assert "README.md" in result.state_snapshot.get("diff_stat", "")
