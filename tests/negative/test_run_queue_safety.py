"""NEG-2026-09-19 · A-run safety: queue order, non-destructive reconcile.

Live incident (awf-bug-report-arun-home.md): the first real run archived
three queued TODOs into done/ before any work (R2), ran the LAST queue item
first because the engine picks "newest active TODO" (R1), and reported a
confusing position (R4). These are data-destruction class bugs — fix first,
then never again.

RED-first contract: every test here failed before the fix.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import api, run_state


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QueueSafety")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str, body: str = "# Task\n") -> None:
    """Put a TODO into the inbox as ACTIVE (md + ready), like the supervisor does."""
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


class TestQueuePinning:
    """R1: the pipeline must run THE QUEUE ITEM, not the newest active TODO."""

    def test_plan_stage_keeps_pinned_todo(self, tmp_git_repo, monkeypatch):
        import awf.pipeline_engine as engine
        from awf.pipeline import Stage

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0015")
        _activate(proj, "TODO-0018")  # newer active — the engine used to prefer it

        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "")

        plan = Stage(name="plan", role="supervisor", kind="plan")
        new_todo, delta, rc = engine.execute_supervisor_stage(
            plan, "TODO-0015", auto=True, project_dir=proj,
            config={}, logs_dir=proj / ".agentic" / "logs", pipeline_name=None,
        )

        assert (delta, rc) == (1, 0)
        assert new_todo == "TODO-0015", "queue order must win over 'newest active'"

    def test_run_next_pins_todo_into_start_pipeline(self, tmp_git_repo, monkeypatch):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0015")
        _activate(proj, "TODO-0018")
        api.run_start(proj, queue=["TODO-0015", "TODO-0018"])

        captured: dict = {}

        def fake_start(project_dir, **kw):
            captured.update(kw)
            return api_pipeline.StartResult(
                run_mode="background", run_id=1,
                log_file=str(proj / "log.txt"), exit_code=0, message="ok",
            )

        monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

        result = api.run_next(proj)

        assert result.action == "started"
        assert captured.get("todo_id") == "TODO-0015"


class TestReconcileNonDestructive:
    """R2: reconcile must NOT archive active TODOs — the queue is legitimate."""

    def test_multiple_active_left_alone(self, tmp_git_repo):
        from awf.api.pipeline import _reconcile

        proj = _project(tmp_git_repo)
        for tid in ("TODO-0015", "TODO-0016", "TODO-0017", "TODO-0018"):
            _activate(proj, tid)

        _reconcile(proj)

        inbox = proj / ".agentic" / "inbox"
        for tid in ("TODO-0015", "TODO-0016", "TODO-0017", "TODO-0018"):
            assert (inbox / f"{tid}.md").is_file(), f"{tid} must stay in inbox"
            assert (inbox / f"{tid}.ready").is_file(), f"{tid}.ready must survive"
        done = proj / ".agentic" / "done"
        archived = [d.name for d in done.iterdir()] if done.is_dir() else []
        assert archived == [], f"nothing may be archived by reconcile, got {archived}"


class TestRunPosition:
    """R4: position = the RUNNING item's number, not 'next to run'."""

    def test_position_counts_running_item(self):
        state = {"queue": ["TODO-0001", "TODO-0002", "TODO-0003", "TODO-0004"],
                 "index": 1, "current": "TODO-0001"}
        assert run_state.position(state) == "1/4"

    def test_position_before_launch(self):
        state = {"queue": ["TODO-0001", "TODO-0002"], "index": 0, "current": ""}
        assert run_state.position(state) == "1/2"

    def test_position_exhausted(self):
        state = {"queue": ["TODO-0001"], "index": 1, "current": "TODO-0001"}
        assert run_state.position(state) == "1/1"


class TestGhostRunGuard:
    """A1: run_start must refuse a non-project dir (the ~/.agentic ghost)."""

    def test_agentic_without_config_is_not_a_project(self, tmp_path):
        ghost = tmp_path / "ghost"
        (ghost / ".agentic" / "state").mkdir(parents=True)  # .agentic, but no config.yaml

        with pytest.raises(api.AwfApiError) as exc:
            api.run_start(ghost, queue=["TODO-0001"])

        assert "config.yaml" in str(exc.value)
        assert str(ghost) in str(exc.value)  # the path we refused — no guessing

    def test_ghost_state_not_written(self, tmp_path):
        ghost = tmp_path / "ghost"
        (ghost / ".agentic" / "state").mkdir(parents=True)

        with pytest.raises(api.AwfApiError):
            api.run_start(ghost, queue=["TODO-0001"])

        assert not (ghost / ".agentic" / "state" / "run.yaml").exists()


class TestMissingTodoDiagnosis:
    """A2: 'missing or empty' must show WHERE it looked."""

    def test_message_contains_searched_path(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=["TODO-0001"])

        result = api.run_next(proj)

        assert result.action == "refused"
        assert str(proj / ".agentic" / "inbox" / "TODO-0001.md") in result.message


class TestRestoreTodo:
    """R2a safety net: an archived TODO can be brought back."""

    def test_roundtrip_archive_restore(self, tmp_git_repo):
        from awf import todos

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0015", body="# Task 15\n\nreal work\n")
        handoff = proj / ".agentic" / "handoff"
        handoff.mkdir(parents=True, exist_ok=True)
        (handoff / "agent-x-TODO-0015.md").write_text("# h\n", encoding="utf-8")

        todos.archive_todo(proj, "TODO-0015")
        inbox = proj / ".agentic" / "inbox"
        assert not (inbox / "TODO-0015.md").exists()

        api.restore_todo(proj, "TODO-0015")

        assert (inbox / "TODO-0015.md").read_text(encoding="utf-8") == "# Task 15\n\nreal work\n"
        assert (inbox / "TODO-0015.ready").is_file(), "restored TODO must be active again"
        assert (handoff / "agent-x-TODO-0015.md").is_file()

    def test_restore_missing_raises(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        with pytest.raises(api.AwfApiError, match="done/TODO-9999"):
            api.restore_todo(proj, "TODO-9999")
