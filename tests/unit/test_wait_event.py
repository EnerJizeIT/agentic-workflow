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

    def test_blocked_event_returns_immediately(self, awf_project):
        """When last_signal starts with BLOCKED → returns 'blocked'."""
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
            last_signal="BLOCKED-TODO-0001",
        )
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "blocked"
        assert "BLOCKED-TODO-0001" in result.message

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
