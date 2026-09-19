"""Tests for extracted transition handlers (_handle_next, _handle_escalate, _handle_rollback)."""
from __future__ import annotations

from awf.pipeline import Stage
from awf.pipeline_engine import _handle_escalate, _handle_next, _handle_rollback


def _make_stage(name="implement", role="worker", max_retries=3):
    return Stage(
        name=name, role=role, kind="execute",
        max_retries=max_retries, on_blocked="escalate",
    )


class TestHandleNext:

    def test_advances_stage_idx(self, tmp_path, monkeypatch):
        """_handle_next returns stage_idx + 1."""
        monkeypatch.setattr("awf.pipeline_engine._maybe_commit", lambda *a, **kw: None)
        monkeypatch.setattr("awf.pipeline_engine._read_baseline_sha", lambda *a: "")
        new_idx = _handle_next(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            action="next", auto=False,
            retry_counts=[0, 0, 0], stage_idx=1,
        )
        assert new_idx == 2

    def test_resets_retry_count(self, tmp_path, monkeypatch):
        """_handle_next resets retry_counts[stage_idx] to 0."""
        monkeypatch.setattr("awf.pipeline_engine._maybe_commit", lambda *a, **kw: None)
        monkeypatch.setattr("awf.pipeline_engine._read_baseline_sha", lambda *a: "")
        retry_counts = [0, 2, 0]  # stage 1 had 2 retries
        _handle_next(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            action="commit_and_next", auto=False,
            retry_counts=retry_counts, stage_idx=1,
        )
        assert retry_counts[1] == 0


class TestHandleEscalate:

    def test_returns_exit_1_on_max_retries(self, tmp_path, monkeypatch):
        """When retry_counts >= max_retries, escalate stops pipeline."""
        stage = _make_stage(max_retries=2)
        idx, todo, exit_code = _handle_escalate(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            auto=False, stage=stage,
            retry_counts=[0, 2, 0],  # already at max
            stage_idx=1,
        )
        assert exit_code == 1

    def test_increments_retry_count(self, tmp_path, monkeypatch):
        """Successful escalate increments retry_counts[stage_idx]."""
        monkeypatch.setattr("awf.pipeline_engine._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.pipeline_engine._find_active_todo", lambda *a: "TODO-0002")
        stage = _make_stage(max_retries=3)
        retry_counts = [0, 0, 0]
        idx, todo, exit_code = _handle_escalate(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            auto=False, stage=stage,
            retry_counts=retry_counts, stage_idx=1,
        )
        assert exit_code == 0
        assert retry_counts[1] == 1
        # Stage idx unchanged (retry same stage)
        assert idx == 1
        # New TODO from supervisor
        assert todo == "TODO-0002"

    def test_exit_1_when_no_new_todo(self, tmp_path, monkeypatch):
        """If supervisor didn't create new TODO, exit 1."""
        monkeypatch.setattr("awf.pipeline_engine._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.pipeline_engine._find_active_todo", lambda *a: "")
        stage = _make_stage(max_retries=3)
        idx, todo, exit_code = _handle_escalate(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            auto=False, stage=stage,
            retry_counts=[0, 0, 0], stage_idx=1,
        )
        assert exit_code == 1

    def test_replan_timeout_stops_cleanly(self, tmp_path, monkeypatch):
        """AUD04-05: a supervisor replan timeout on the escalate path must
        stop the pipeline (rc=1, logged) — not escape as an uncaught
        TimeoutError traceback that leaves the state dirty."""
        def boom(*a, **kw):
            raise TimeoutError("Supervisor replan signal not received within 5s")

        monkeypatch.setattr("awf.pipeline_engine._run_supervisor_stage", boom)
        stage = _make_stage(max_retries=3)
        idx, todo, exit_code = _handle_escalate(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            auto=False, stage=stage,
            retry_counts=[0, 0, 0], stage_idx=1,
        )
        assert exit_code == 1, "replan timeout must stop the pipeline, not crash"
        assert idx == 1
        assert todo == "TODO-0001"


class TestHandleRollback:

    def test_returns_target_idx(self, tmp_path, monkeypatch):
        """_handle_rollback returns target_idx."""
        monkeypatch.setattr("awf.pipeline_engine._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.pipeline_engine._find_active_todo", lambda *a: "TODO-0003")
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="implement", role="worker", kind="execute"),
            Stage(name="verify", role="supervisor", kind="verify"),
        ]
        idx, todo, exit_code = _handle_rollback(
            project_dir=tmp_path, logs_dir=tmp_path,
            stages=stages, current_todo="TODO-0001",
            auto=False, target="implement",
        )
        assert exit_code == 0
        assert idx == 1  # implement is at index 1
        assert todo == "TODO-0003"

    def test_exit_1_when_target_not_found(self, tmp_path):
        stages = [Stage(name="plan", role="supervisor", kind="plan")]
        idx, todo, exit_code = _handle_rollback(
            project_dir=tmp_path, logs_dir=tmp_path,
            stages=stages, current_todo="TODO-0001",
            auto=False, target="nonexistent",
        )
        assert exit_code == 1

    def test_replan_timeout_stops_cleanly(self, tmp_path, monkeypatch):
        """AUD04-05: a supervisor replan timeout on the rollback path must
        stop the pipeline (rc=1, logged) — not escape as an uncaught
        TimeoutError traceback."""
        def boom(*a, **kw):
            raise TimeoutError("Supervisor replan signal not received within 5s")

        monkeypatch.setattr("awf.pipeline_engine._run_supervisor_stage", boom)
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            Stage(name="implement", role="worker", kind="execute"),
            Stage(name="verify", role="supervisor", kind="verify"),
        ]
        idx, todo, exit_code = _handle_rollback(
            project_dir=tmp_path, logs_dir=tmp_path,
            stages=stages, current_todo="TODO-0001",
            auto=False, target="implement",
        )
        assert exit_code == 1, "rollback replan timeout must stop, not crash"


class TestPipelineEngineUnpacking:
    """Regression: DAUD-7 re-audit found swapped unpacking in execute_agent_stage.

    _handle_escalate/_handle_rollback return (int, str, int) = (idx, todo, exit).
    execute_agent_stage must return (str, int, int) = (todo, idx, exit).
    """

    def test_escalate_return_order(self, tmp_path, monkeypatch):
        """execute_agent_stage passes escalate return in correct order."""
        from awf import pipeline_engine

        # Mock prerequisites
        monkeypatch.setattr(
            pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: []
        )
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", lambda *a, **kw: None)

        # Write BLOCKED signal
        outbox = tmp_path / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "BLOCKED-TODO-0001.md").write_text("# Blocked\nNeed help\n")
        (outbox / "BLOCKED-TODO-0001.ready").touch()

        # Mock _handle_escalate to return sentinel (int, str, int)
        monkeypatch.setattr(
            pipeline_engine,
            "_handle_escalate",
            lambda *a, **kw: (42, "TODO-0099", 0),
        )

        stage = _make_stage()
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            stage,
            Stage(name="verify", role="supervisor", kind="verify"),
        ]

        todo, idx, exit_code = pipeline_engine.execute_agent_stage(
            stage=stage,
            current_todo="TODO-0001",
            project_dir=tmp_path,
            config={},
            logs_dir=tmp_path,
            stages=stages,
            stage_idx=1,
            retry_counts=[0, 0, 0],
            auto=False,
            agent_hard_timeout=None,
        )

        # Must be (str, int, int) — not (int, str, int)
        assert todo == "TODO-0099"
        assert idx == 42
        assert exit_code == 0
        assert isinstance(todo, str)
        assert isinstance(idx, int)

    def test_rollback_return_order(self, tmp_path, monkeypatch):
        """execute_agent_stage passes rollback return in correct order."""
        from awf import pipeline_engine

        monkeypatch.setattr(
            pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: []
        )
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", lambda *a, **kw: None)

        # Write REVIEW-REJECTED signal (triggers rollback)
        outbox = tmp_path / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "REVIEW-REJECTED-TODO-0001.md").write_text("# Rejected\nBad code\n")
        (outbox / "REVIEW-REJECTED-TODO-0001.ready").touch()

        monkeypatch.setattr(
            pipeline_engine,
            "_handle_rollback",
            lambda *a, **kw: (7, "TODO-0088", 0),
        )

        stage = Stage(
            name="implement", role="worker", kind="execute",
            on_rejected="rollback_to:implement",
        )
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            stage,
            Stage(name="verify", role="supervisor", kind="verify"),
        ]

        todo, idx, exit_code = pipeline_engine.execute_agent_stage(
            stage=stage,
            current_todo="TODO-0001",
            project_dir=tmp_path,
            config={},
            logs_dir=tmp_path,
            stages=stages,
            stage_idx=1,
            retry_counts=[0, 0, 0],
            auto=False,
            agent_hard_timeout=None,
        )

        assert todo == "TODO-0088"
        assert idx == 7
        assert exit_code == 0
        assert isinstance(todo, str)
        assert isinstance(idx, int)
