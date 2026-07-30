"""Tests for extracted transition handlers (_handle_next, _handle_escalate, _handle_rollback)."""
from __future__ import annotations

from awf.orchestrator import _handle_escalate, _handle_next, _handle_rollback
from awf.pipeline import Stage


def _make_stage(name="implement", role="worker", max_retries=3):
    return Stage(
        name=name, role=role, kind="execute",
        max_retries=max_retries, on_blocked="escalate",
    )


class TestHandleNext:

    def test_advances_stage_idx(self, tmp_path, monkeypatch):
        """_handle_next returns stage_idx + 1."""
        monkeypatch.setattr("awf.orchestrator._maybe_commit", lambda *a, **kw: None)
        monkeypatch.setattr("awf.orchestrator._read_baseline_sha", lambda *a: "")
        new_idx = _handle_next(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            action="next", auto=False,
            retry_counts=[0, 0, 0], stage_idx=1,
        )
        assert new_idx == 2

    def test_resets_retry_count(self, tmp_path, monkeypatch):
        """_handle_next resets retry_counts[stage_idx] to 0."""
        monkeypatch.setattr("awf.orchestrator._maybe_commit", lambda *a, **kw: None)
        monkeypatch.setattr("awf.orchestrator._read_baseline_sha", lambda *a: "")
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
        monkeypatch.setattr("awf.orchestrator._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.orchestrator._find_active_todo", lambda *a: "TODO-0002")
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
        monkeypatch.setattr("awf.orchestrator._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.orchestrator._find_active_todo", lambda *a: "")
        stage = _make_stage(max_retries=3)
        idx, todo, exit_code = _handle_escalate(
            project_dir=tmp_path, logs_dir=tmp_path,
            s_name="implement", current_todo="TODO-0001",
            auto=False, stage=stage,
            retry_counts=[0, 0, 0], stage_idx=1,
        )
        assert exit_code == 1


class TestHandleRollback:

    def test_returns_target_idx(self, tmp_path, monkeypatch):
        """_handle_rollback returns target_idx."""
        monkeypatch.setattr("awf.orchestrator._run_supervisor_stage", lambda *a, **kw: None)
        monkeypatch.setattr("awf.orchestrator._find_active_todo", lambda *a: "TODO-0003")
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
