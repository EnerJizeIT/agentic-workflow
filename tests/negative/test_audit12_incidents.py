"""AUD12-04 / AUD12-05: regression tests for the audited incidents.

Each test reproduces a real incident from the audit reports
(~/Desktop/awf-audit/reports/) and asserts the SAFE behavior. On the
current code these tests are RED — that is the point of the suite: the
audited bug must be caught by the test suite, not by a live incident.

Findings covered:
- T3: AUD05-01, AUD04-02, AUD03-01, AUD04-01, AUD10-01
- T4: AUD04-04, AUD05-02, AUD05-06, AUD02-02
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from awf import api, run_state
from awf import pipeline_engine as engine
from awf.pipeline import Stage


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Incidents")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str, body: str = "# Task\n") -> None:
    """Put a TODO into the inbox as ACTIVE (md + ready)."""
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _bare_project(tmp_path: Path) -> Path:
    """Minimal .agentic skeleton (no init_project) for engine-level tests."""
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "context", "logs", "state", "handoff"):
        (proj / ".agentic" / d).mkdir(parents=True)
    return proj


# ── T3.1 · AUD05-01: init_project(dry_run=True) must not touch the FS ──


class TestInitDryRun:
    def test_init_dry_run_preserves_runtime(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        inbox = proj / ".agentic" / "inbox"
        state = proj / ".agentic" / "state"
        done = proj / ".agentic" / "done"

        def _seed_runtime():
            (inbox / "TODO-0001.md").write_text("# active task\n", encoding="utf-8")
            (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
            state.mkdir(parents=True, exist_ok=True)
            (state / "current.yaml").write_text("phase: run\n", encoding="utf-8")
            (done / "TODO-0000").mkdir(parents=True, exist_ok=True)
            (done / "TODO-0000" / "TODO.md").write_text("archived\n", encoding="utf-8")

        _seed_runtime()
        api.init_project(proj, dry_run=True)

        # dry_run must be a pure read: every runtime file survives
        assert (inbox / "TODO-0001.md").is_file(), "dry-run deleted the active TODO"
        assert (inbox / "TODO-0001.ready").is_file(), "dry-run deleted the .ready signal"
        assert (state / "current.yaml").is_file(), "dry-run deleted stage state"
        assert (done / "TODO-0000" / "TODO.md").is_file(), "dry-run deleted the done/ archive"

        # R1 pin: the REAL (non-dry) re-init still cleans runtime — documented behavior
        _seed_runtime()
        api.init_project(proj, dry_run=False)
        assert not (inbox / "TODO-0001.md").exists(), "R1: re-init must clean inbox"
        assert not (state / "current.yaml").exists(), "R1: re-init must clean state"
        assert not (done / "TODO-0000").exists(), "R1: re-init must clean done/"


# ── T3.2 · AUD04-02: on_blocked=stop must stop (rc=1), not loop ──


class TestOnBlockedStop:
    def test_on_blocked_stop_no_loop(self, tmp_path, monkeypatch):
        proj = _bare_project(tmp_path)
        outbox = proj / ".agentic" / "outbox"
        runs = {"n": 0}

        def fake_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            runs["n"] += 1
            (outbox / f"BLOCKED-{todo_id}.md").write_text("cannot proceed\n", encoding="utf-8")
            (outbox / f"BLOCKED-{todo_id}.ready").write_text("", encoding="utf-8")

        monkeypatch.setattr(engine, "_run_agent_stage", fake_agent)
        monkeypatch.setattr(engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(engine, "_read_baseline_sha", lambda *a, **kw: "")
        monkeypatch.setattr(engine, "wait_for_signal", lambda *a, **kw: None)

        stage = Stage(name="impl", role="developer", kind="execute", on_blocked="stop")
        stages = [stage]
        todo, stage_idx, rc = engine.execute_agent_stage(
            stage, "TODO-0001", proj, {}, proj / ".agentic" / "logs",
            stages, 0, [0], True, None,
        )

        assert runs["n"] == 1, f"worker launched {runs['n']} times — the stop loop"
        assert rc == 1, "on_blocked=stop must return rc=1 so the orchestrator exits"


# ── T3.3 · AUD03-01: rollback must have a budget ──


class TestRollbackBudget:
    def test_rollback_has_budget(self, tmp_path, monkeypatch):
        proj = _bare_project(tmp_path)
        _activate(proj, "TODO-0001")
        logs = proj / ".agentic" / "logs"

        monkeypatch.setattr(engine, "_run_supervisor_stage", lambda *a, **kw: "")

        stages = [
            Stage(name="implement", role="developer", kind="execute"),
            Stage(name="verify", role="supervisor", kind="verify",
                  on_rejected="rollback_to:implement"),
        ]

        results = []
        for _ in range(12):
            results.append(engine._handle_rollback(
                proj, logs, stages, "TODO-0001", True, "implement",
            ))
            if results[-1][2] != 0:
                break

        first_idx, first_todo, first_rc = results[0]
        assert (first_idx, first_rc) == (0, 0), "the FIRST rollback must still work"
        assert any(rc != 0 for _, _, rc in results), (
            "repeated rollbacks are unbounded — the loop never stops"
        )
        assert len(results) <= 6, (
            f"budget must stop the loop within a few rollbacks, got {len(results)}"
        )


# ── T3.4 · AUD04-01: continue must pass todo_id from state ──


class TestContinuePassesTodoId:
    def test_continue_passes_todo_id(self, tmp_git_repo, monkeypatch):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")

        from awf.pipeline_state import write_state

        write_state(proj, stage_name="verify", stage_kind="verify", todo_id="TODO-0001")

        captured: dict = {}

        def fake_start(project_dir, **kw):
            captured.update(kw)
            return os.getpid(), proj / ".agentic" / "logs" / "awf-start.out", proj / ".agentic" / "logs" / "awf-start.pid"

        monkeypatch.setattr(api_pipeline, "start_in_background", fake_start)
        monkeypatch.setattr(api_pipeline, "_verify_child_alive", lambda pid, log_file=None: True)

        result = api_pipeline.continue_pipeline(proj, background=True)

        assert result.run_mode == "background"
        assert captured.get("todo_id") == "TODO-0001", (
            "continue_pipeline must pass the state's todo_id to the launch — "
            "verify-waiting with an empty todo_id skips the ACK/APPROVE check"
        )


# ── T3.5 · AUD10-01: dashboard must not show 'running' without a live stage ──


class TestDashboardStatus:
    def test_dashboard_status_not_running_without_stage(self):
        from awf.api.dashboard import _determine_status

        done_status = _determine_status({"phase": "done"})
        assert done_status[0] != "running", (
            "phase=done with no stage/pid must not render as 'running'"
        )

        bare_run = _determine_status({"phase": "run"})
        assert bare_run[0] != "running", (
            "no stage, no pid, no signal — a dead state must not render as 'running'"
        )


# ── T4.1 · AUD04-04: stale APPROVE must not be accepted by a new verify ──


class TestStaleApprove:
    def test_stale_approve_not_consumed(self, tmp_path):
        from awf import supervisor

        proj = _bare_project(tmp_path)
        stale = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
        stale.write_text("", encoding="utf-8")
        old = time.time() - 3600  # a cycle ago
        os.utime(stale, (old, old))

        try:
            result = supervisor.wait_for_supervisor_signal(
                "verify", "TODO-0001", proj, proj / ".agentic" / "logs",
                poll_interval=0.2, timeout=1,
            )
        except TimeoutError:
            return  # correct: stale signal ignored, the wait timed out

        pytest.fail(
            f"APPROVE from a previous cycle (mtime {3600}s old) was accepted "
            f"by a fresh verify wait: {result!r}"
        )


# ── T4.2 · AUD05-02: reset must clear the run, not leave a ghost ──


class TestResetClearsRun:
    def test_reset_clears_run(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")

        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])
        assert run_state.read_run(proj) is not None

        api.reset_runtime(proj)

        assert run_state.read_run(proj) is None, (
            "reset left state/run.yaml — a ghost run blocks the next run_start"
        )
        # and a fresh run can start cleanly
        api.run_start(proj, queue=["TODO-0001", "TODO-0002"])
        assert run_state.read_run(proj)["active"] is True


# ── T4.3 · AUD05-06: orphans must not be removable while the pipeline is alive ──


class TestOrphansGuard:
    def test_orphans_guard_live_pipeline(self, tmp_git_repo, monkeypatch):
        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")  # no PROGRESS — looks like an orphan
        _activate(proj, "TODO-0002")
        inbox = proj / ".agentic" / "inbox"
        pid_file = proj / ".agentic" / "logs" / "awf-start.pid"

        # live pipeline (this pytest process is alive and runs python);
        # AUD04-07 strict identity — make it look like an awf pipeline
        # via the read_cmdline seam.
        from awf.api import _liveness
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        monkeypatch.setattr(
            _liveness, "read_cmdline",
            lambda pid: "python\x00-m\x00awf\x00start\x00" if pid == os.getpid() else None,
        )
        try:
            try:
                api.remove_orphans(proj, ["TODO-0001"])
            except api.AwfApiError:
                pass  # refusal via exception is an acceptable form
            assert (inbox / "TODO-0001.md").is_file(), (
                "remove_orphans deleted the running TODO while the pipeline is alive"
            )
        finally:
            pid_file.unlink()

        # no live pipeline — the honest orphan is removable (pin current behavior)
        result = api.remove_orphans(proj, ["TODO-0002"])
        assert "TODO-0002" in result.orphan_ids
        assert not (inbox / "TODO-0002.md").exists()


# ── T4.4 · AUD02-02: failed launch must not move the run position ──


class TestRunNextFailedStart:
    def test_run_next_failed_start_keeps_position(self, tmp_git_repo, monkeypatch):
        import awf.api.pipeline as api_pipeline

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0101")
        _activate(proj, "TODO-0102")
        api.run_start(proj, queue=["TODO-0101", "TODO-0102"])
        before = run_state.read_run(proj)

        def fake_start(project_dir, **kw):
            return api_pipeline.StartResult(
                run_mode="error", run_id=None, log_file=None, exit_code=1,
                message="child died immediately",
            )

        monkeypatch.setattr(api_pipeline, "start_pipeline", fake_start)

        result = api.run_next(proj)
        after = run_state.read_run(proj)

        assert result.action == "refused"
        assert after["index"] == before["index"], (
            "index moved before the launch — the queue item was skipped"
        )
        assert after["current"] == before["current"], "current moved before the launch"
        assert after["completed"] == before["completed"], (
            "completed moved before the launch — an unlaunched TODO counts as done"
        )

        # the retry must try the SAME item again, not the next one
        result2 = api.run_next(proj)
        assert result2.todo_id == "TODO-0101", (
            f"retry tried {result2.todo_id} — the unlaunched TODO-0101 is lost"
        )
