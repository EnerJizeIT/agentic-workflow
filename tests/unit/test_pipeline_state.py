"""T4.1: Pipeline state persistence — unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

from awf import pipeline_state


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ structure."""
    return tmp_git_repo


class TestWriteReadState:
    def test_write_creates_state_file(self, awf_project):
        """write_state creates .agentic/state/current.yaml."""
        pipeline_state.write_state(
            awf_project,
            stage_name="agent-developer",
            stage_kind="execute",
            stage_idx=2,
        )

        state_path = awf_project / ".agentic" / "state" / "current.yaml"
        assert state_path.is_file()

        data = yaml.safe_load(state_path.read_text())
        assert data["stage_name"] == "agent-developer"
        assert data["stage_kind"] == "execute"
        assert data["stage_idx"] == 2

    def test_read_returns_none_when_missing(self, awf_project):
        """No state file → read_state returns None."""
        assert pipeline_state.read_state(awf_project) is None

    def test_write_merges_with_existing(self, awf_project):
        """Second write_state merges fields (doesn't overwrite entire file)."""
        pipeline_state.write_state(awf_project, stage_name="plan", stage_kind="plan")
        pipeline_state.write_state(awf_project, stage_idx=0, todo_id="TODO-0001")

        data = pipeline_state.read_state(awf_project)
        assert data["stage_name"] == "plan"  # preserved from first write
        assert data["stage_idx"] == 0  # from second write
        assert data["todo_id"] == "TODO-0001"

    def test_write_overwrites_changed_fields(self, awf_project):
        """Same key in second write overwrites first."""
        pipeline_state.write_state(awf_project, stage_name="plan")
        pipeline_state.write_state(awf_project, stage_name="execute")

        data = pipeline_state.read_state(awf_project)
        assert data["stage_name"] == "execute"

    def test_write_adds_updated_at(self, awf_project):
        """Each write adds updated_at timestamp for staleness check."""
        pipeline_state.write_state(awf_project, stage_name="x")
        data = pipeline_state.read_state(awf_project)
        assert "updated_at" in data
        # Must be parseable ISO format
        ts = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
        assert ts.tzinfo is not None


class TestClearState:
    def test_clear_removes_file(self, awf_project):
        """clear_state deletes state file."""
        pipeline_state.write_state(awf_project, stage_name="x")
        state_path = awf_project / ".agentic" / "state" / "current.yaml"
        assert state_path.is_file()

        pipeline_state.clear_state(awf_project)
        assert not state_path.is_file()

    def test_clear_idempotent(self, awf_project):
        """clear_state on missing file doesn't raise."""
        pipeline_state.clear_state(awf_project)  # no file yet
        pipeline_state.clear_state(awf_project)  # still OK


class TestIsStale:
    def test_fresh_state_not_stale(self, awf_project):
        """State written just now → not stale."""
        pipeline_state.write_state(awf_project, stage_name="x")
        data = pipeline_state.read_state(awf_project)
        assert not pipeline_state.is_state_stale(data)

    def test_old_state_is_stale(self, awf_project):
        """State with updated_at 3 hours ago → stale."""
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        data = {"stage_name": "x", "updated_at": old_ts}
        assert pipeline_state.is_state_stale(data, max_age_seconds=7200)

    def test_missing_updated_at_is_stale(self, awf_project):
        """No updated_at field → treat as stale."""
        data = {"stage_name": "x"}
        assert pipeline_state.is_state_stale(data)

    def test_corrupt_timestamp_is_stale(self, awf_project):
        """Unparseable timestamp → stale."""
        data = {"stage_name": "x", "updated_at": "not-a-timestamp"}
        assert pipeline_state.is_state_stale(data)

    def test_z_suffix_parsed(self, awf_project):
        """Python 3.9 compat: Z suffix handled by fromisoformat replacement."""
        recent_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        data = {"stage_name": "x", "updated_at": recent_ts}
        assert not pipeline_state.is_state_stale(data)


class TestCheckpointStateFields:
    """Verify checkpoint-specific fields round-trip through write/read."""

    def test_checkpoint_pending_written_and_read(self, awf_project):
        pipeline_state.write_state(
            awf_project,
            checkpoint_pending=True,
            checkpoint_port=42123,
            checkpoint_form_url="file:///tmp/checkpoint.html",
        )
        data = pipeline_state.read_state(awf_project)
        assert data["checkpoint_pending"] is True
        assert data["checkpoint_port"] == 42123
        assert data["checkpoint_form_url"] == "file:///tmp/checkpoint.html"

    def test_checkpoint_resolved_clears_pending(self, awf_project):
        pipeline_state.write_state(awf_project, checkpoint_pending=True)
        pipeline_state.write_state(awf_project, checkpoint_pending=False)
        data = pipeline_state.read_state(awf_project)
        assert data["checkpoint_pending"] is False


class TestExtractStageInfoPrefersStateFile:
    """T4.1: _extract_stage_info reads state file FIRST, regex as fallback."""

    def test_state_file_used_when_present(self, awf_project):
        """When state file exists and not stale — structured fields returned."""
        from awf.api.context import _extract_stage_info

        pipeline_state.write_state(
            awf_project,
            stage_name="agent-system-analyst",
            stage_kind="execute",
            stage_idx=1,
            checkpoint_pending=True,
            checkpoint_form_url="file:///tmp/form.html",
            checkpoint_port=12345,
        )

        cur, nxt, sig, _lt, cp, port, url = _extract_stage_info(awf_project)
        assert cur == "agent-system-analyst"
        assert cp is True
        assert port == 12345
        assert url == "file:///tmp/form.html"

    def test_no_state_file_returns_none(self, awf_project):
        """AUD-12.5: No state file → None values (regex fallback removed)."""
        from awf.api.context import _extract_stage_info

        cur, _nxt, _sig, _lt, cp, _port, _url = _extract_stage_info(awf_project)
        assert cur is None
        assert cp is False

    def test_stale_state_returns_none(self, awf_project):
        """AUD-12.5: Stale state file → None values (regex fallback removed)."""
        from awf.api.context import _extract_stage_info

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        state_path = awf_project / ".agentic" / "state"
        state_path.mkdir(parents=True, exist_ok=True)
        (state_path / "current.yaml").write_text(
            f"stage_name: stale-stage\nupdated_at: {old_ts}\n"
        )

        cur, _nxt, _sig, _lt, _cp, _port, _url = _extract_stage_info(awf_project)
        assert cur is None  # stale state NOT used, no regex fallback


class TestWriteStateConcurrency:
    """AUD05-05: parallel write_state must not lose fields (same lock)."""

    def test_parallel_writes_no_lost_fields(self, awf_project):
        import threading

        proj = awf_project
        iters = 50
        errors: list = []

        def writer(prefix: str) -> None:
            for i in range(iters):
                try:
                    pipeline_state.write_state(proj, **{f"field_{prefix}_{i}": i})
                except Exception as e:  # noqa: BLE001 — surface in the test
                    errors.append(e)

        t1 = threading.Thread(target=writer, args=("x",))
        t2 = threading.Thread(target=writer, args=("y",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, errors
        state = pipeline_state.read_state(proj) or {}
        for i in range(iters):
            assert f"field_x_{i}" in state, f"lost field field_x_{i}"
            assert f"field_y_{i}" in state, f"lost field field_y_{i}"
