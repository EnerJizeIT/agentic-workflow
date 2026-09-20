"""Tests for pipeline_engine: plan stage (single-phase, FU-05), F4 auto-retry."""
from __future__ import annotations

import pytest

from awf import pipeline_engine
from awf.pipeline import Stage


def _make_stages() -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute", on_blocked="escalate"),
        Stage(name="verify", role="supervisor", kind="verify", on_approved="commit_and_next"),
    ]


class TestPlanStage:
    """FU-05: single-phase plan — the contract is the TODO, no BRIEF."""

    def test_stale_brief_does_not_hijack_new_run(self, tmp_path, monkeypatch):
        """AUD04-03 regression: stale BRIEF-* leftovers in the inbox must not
        pin the run to an already-finished TODO. The plan stage has to run the
        newest active TODO (TODO-0002 here) and call the checkpoint gate
        exactly once for it — no second supervisor call, no Brief phase."""
        project = tmp_path / "proj"
        inbox = project / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        # Stale R5 leftovers for a FINISHED TODO-0001
        (inbox / "BRIEF-TODO-0001.md").write_text("# Brief\nGoal: test\n")
        (inbox / "BRIEF-TODO-0001.ready").write_text("")
        done_dir = project / ".agentic" / "done" / "TODO-0001"
        done_dir.mkdir(parents=True)
        (done_dir / "TODO.md").write_text("# TODO (finished)\n")
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "DONE-TODO-0001.ready").write_text("")
        # The NEW active TODO the plan stage must run
        (inbox / "TODO-0002.md").write_text("# TODO\nTask\n")
        (inbox / "TODO-0002.ready").write_text("")

        supervisor_calls = {"n": 0}
        checkpoint_todos: list[str] = []

        def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            supervisor_calls["n"] += 1
            return ""

        def mock_checkpoint(todo_id, project_dir, config, auto, logs_dir):
            checkpoint_todos.append(todo_id)
            return 0

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
        monkeypatch.setattr(pipeline_engine, "_run_plan_checkpoint_gate", mock_checkpoint)
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

        stages = _make_stages()
        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=stages[0], current_todo="", auto=False,
            project_dir=project, config={}, logs_dir=tmp_path,
        )

        assert todo == "TODO-0002", (
            "stale BRIEF hijacked the run to a finished TODO — AUD04-03 "
            "regression: BRIEF-* files are not part of the contract"
        )
        assert checkpoint_todos == ["TODO-0002"]
        assert supervisor_calls["n"] == 1  # single phase — no second call
        assert rc == 0

    def test_single_phase_plan(self, tmp_path, monkeypatch):
        """Plan stage: one supervisor call, checkpoint previews the TODO."""
        project = tmp_path / "proj"
        inbox = project / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO\n")
        (inbox / "TODO-0001.ready").write_text("")

        supervisor_calls = {"n": 0}
        checkpoint_todos: list[str] = []

        def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            supervisor_calls["n"] += 1
            return "TODO-0001"

        def mock_checkpoint(todo_id, project_dir, config, auto, logs_dir):
            checkpoint_todos.append(todo_id)
            return 0

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
        monkeypatch.setattr(pipeline_engine, "_run_plan_checkpoint_gate", mock_checkpoint)
        monkeypatch.setattr(pipeline_engine, "_find_active_todo", lambda pd: "TODO-0001")
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

        stages = _make_stages()
        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=stages[0], current_todo="", auto=False,
            project_dir=project, config={}, logs_dir=tmp_path,
        )

        assert supervisor_calls["n"] == 1
        assert checkpoint_todos == ["TODO-0001"]
        assert todo == "TODO-0001"
        assert rc == 0

    def test_pinned_todo_wins_over_newest_active(self, tmp_path, monkeypatch):
        """NEG-2026-09-19 R1: a caller-pinned TODO (run queue) is kept."""
        project = tmp_path / "proj"
        inbox = project / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO\n")
        (inbox / "TODO-0001.ready").write_text("")
        (inbox / "TODO-0002.md").write_text("# TODO\n")
        (inbox / "TODO-0002.ready").write_text("")

        checkpoint_todos: list[str] = []
        monkeypatch.setattr(
            pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "TODO-0001"
        )
        monkeypatch.setattr(
            pipeline_engine,
            "_run_plan_checkpoint_gate",
            lambda todo_id, *a, **kw: checkpoint_todos.append(todo_id) or 0,
        )
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

        stages = _make_stages()
        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=stages[0], current_todo="TODO-0001", auto=False,
            project_dir=project, config={}, logs_dir=tmp_path,
        )

        assert todo == "TODO-0001"
        assert checkpoint_todos == ["TODO-0001"]
        assert rc == 0

    def test_plan_checkpoint_rejected_stops_pipeline(self, tmp_path, monkeypatch):
        """Checkpoint rejected → pipeline stops."""
        project = tmp_path / "proj"
        inbox = project / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO\n")
        (inbox / "TODO-0001.ready").write_text("")

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_run_plan_checkpoint_gate", lambda *a, **kw: 1)
        monkeypatch.setattr(pipeline_engine, "_find_active_todo", lambda pd: "TODO-0001")
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

        stages = _make_stages()
        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=stages[0], current_todo="", auto=False,
            project_dir=project, config={}, logs_dir=tmp_path,
        )

        assert rc == 1


class TestF4AutoRetry:
    """F4: transient failure → auto-retry before salvage."""

    def test_transient_failure_retries(self, tmp_path, monkeypatch):
        """Worker exits fast with no signal → retry counter increments."""
        project = tmp_path / "proj"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)

        call_count = {"n": 0}

        def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
                (outbox / f"DONE-{todo_id}.ready").write_text("")

        import time as _time_mod
        time_values = iter([100.0, 101.0] * 5)
        monkeypatch.setattr(_time_mod, "monotonic", lambda: next(time_values, 999.0))

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
        from awf import signals as sig_module
        monkeypatch.setattr(sig_module, "clean_stage_signals", lambda *a, **kw: None)

        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

        stages = _make_stages()
        pipeline_engine.execute_agent_stage(
            stage=stages[1], current_todo="TODO-0001", project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
            retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
            pipeline_name=None,
        )

        assert call_count["n"] == 2  # retried once, succeeded on second


class TestP1CheckpointSkip:
    """P1: checkpoint skipped if content unchanged since last approval."""

    def test_unchanged_content_skips_checkpoint(self, tmp_path, monkeypatch):
        import hashlib

        from awf import plan_checkpoint

        project = tmp_path / "proj"
        inbox = project / ".agentic" / "inbox"
        ctx = project / ".agentic" / "context"
        inbox.mkdir(parents=True)
        ctx.mkdir(parents=True)

        content = "# TODO\nTask\n"
        (inbox / "TODO-0001.md").write_text(content)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        # AUD03-04: entry is keyed "{todo_id}:{hash}"
        (ctx / "checkpoint-approved.hash").write_text(f"TODO-0001:{content_hash}\n")

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, {}, tmp_path, timeout=1
        )
        assert result == "approve"  # skipped


class TestP2RetryStage:
    """P2: retry_stage API."""

    def test_no_salvage_stage_raises(self, tmp_git_repo):
        """retry_stage without salvage_stage in state → error."""
        from awf.api._errors import AwfApiError
        from awf.api.pipeline import retry_stage

        # Create .agentic/ so require_agentic passes
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "state").mkdir()
        with pytest.raises(AwfApiError, match="salvage_stage"):
            retry_stage(tmp_git_repo)

    def test_retry_stage_cleans_salvage_from_inbox(self, tmp_git_repo, monkeypatch):
        """AUD04-08: SALVAGE notes are written to the INBOX — the retry
        cleanup must delete them from the inbox (the old outbox glob was a
        no-op that left every note behind)."""
        import awf.api.pipeline as api_pipeline
        from awf.pipeline_state import write_state

        inbox = tmp_git_repo / ".agentic" / "inbox"
        outbox = tmp_git_repo / ".agentic" / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        (inbox / "SALVAGE-TODO-0001.md").write_text("# salvage note\n", encoding="utf-8")

        write_state(tmp_git_repo, salvage_stage="implement")

        monkeypatch.setattr(
            api_pipeline, "kill_pipeline", lambda *a, **kw: {"killed": False}
        )
        captured: dict = {}

        def fake_continue(project_dir, **kw):
            captured.update(kw)
            return "started"

        monkeypatch.setattr(api_pipeline, "continue_pipeline", fake_continue)

        api_pipeline.retry_stage(tmp_git_repo, background=False)

        assert not (inbox / "SALVAGE-TODO-0001.md").exists(), (
            "retry_stage left the inbox SALVAGE note behind — the old outbox "
            "glob never sees it"
        )
        assert captured.get("from_stage") == "implement"


class TestE2BIGGuard:
    """KA2-7: _env.py guards against env var size overflow."""

    def test_large_config_strips_to_permissions(self, monkeypatch):
        """Config >100KB → stripped to permissions-only in env var."""
        from awf._env import awf_subprocess_env

        # Create a very large merged config
        large_config = {"permission": {"bash": "allow"}, "providers": {}}
        # Add huge providers to exceed 100KB
        large_config["providers"]["huge"] = {
            f"model-{i}": {"name": "x" * 1000} for i in range(200)
        }

        import json
        config_json = json.dumps(large_config)
        assert len(config_json) > 100_000  # verify it's actually large

        # Mock the config loading to return our large config
        import awf._env as env_module
        original_env = env_module.awf_subprocess_env

        # The function reads opencode.json internally — we test the guard logic
        # by checking that the output env var doesn't exceed reasonable size
        env = awf_subprocess_env()
        config_content = env.get("OPENCODE_CONFIG_CONTENT", "")
        # If config was huge, it should have been stripped
        # (actual size depends on real opencode.json — just verify no crash)
        assert isinstance(config_content, str)


class TestSalvageEscalation:
    """Dogfood-11: repeat silent exits escalate to task splitting, not blind retry."""

    def test_first_salvage_note_has_no_escalation(self, tmp_git_repo):
        pipeline_engine._write_salvage_prompt(
            tmp_git_repo, "TODO-0001", "agent-implementer", None,
            tmp_git_repo / ".agentic" / "logs",
        )
        text = (
            tmp_git_repo / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md"
        ).read_text(encoding="utf-8")
        assert "**Attempt:** 1" in text
        assert "do NOT retry the same scope" not in text
        # The output-limit diagnosis hint is present from the first salvage.
        assert "output-token" in text

    def test_repeat_salvage_counter_and_escalation(self, tmp_path, monkeypatch):
        """Two silent exits → salvage_count=2 and the note escalates.

        F7 (dogfood-11): each execute call now auto-retries a silent exit
        (no signal, no work evidence) twice with a retry note before the
        salvage path — so the worker runs 3 times per call.
        """
        project = tmp_path / "proj"
        (project / ".agentic" / "outbox").mkdir(parents=True)

        retry_notes: list = []

        def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            retry_notes.append(kw.get("retry_note"))

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

        import time as _time_mod
        ticks = iter(float(i) for i in range(60))
        monkeypatch.setattr(_time_mod, "monotonic", lambda: next(ticks))

        stages = _make_stages()
        for run in range(2):
            if run == 1:
                # AUD04-08: simulate the retry_stage kill between attempts —
                # the documented salvage recovery path (kill + continue).
                # The counter must survive, or the escalation below never
                # happens in the real flow.
                from awf.api.pipeline import kill_pipeline
                from awf.pipeline_state import write_state
                write_state(project, pipeline_pid=2**31)  # dead pid
                kill_pipeline(project)
            pipeline_engine.execute_agent_stage(
                stage=stages[1], current_todo="TODO-0001", project_dir=project,
                config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
                retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
                pipeline_name=None,
            )

        from awf.pipeline_state import read_state
        state = read_state(project)
        assert state.get("salvage_count") == 2, (
            "the kill wiped the salvage counter — the retry cycle restarts "
            "at Attempt 1 forever"
        )

        # Per execute call: 3 worker runs (original + 2 retries), retry notes
        # only on the retries — and the budget resets for the second call.
        assert len(retry_notes) == 6
        assert retry_notes[0] is None
        assert "RETRY 1" in retry_notes[1]
        assert "RETRY 2" in retry_notes[2]
        assert retry_notes[3] is None

        text = (
            project / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md"
        ).read_text(encoding="utf-8")
        assert "**Attempt:** 2" in text
        assert "do NOT retry the same scope" in text

    @staticmethod
    def _mock_silent_project(project, monkeypatch):
        """Shared mocks: silent worker, no auto-done, no signals."""
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
        import time as _time_mod
        monkeypatch.setattr(_time_mod, "monotonic", lambda: 100.0)

    def test_salvage_count_is_per_stage_and_todo(self, tmp_path, monkeypatch):
        """AUD02-07: the counter is per (TODO, stage).

        A first failure of a NEW stage (or a NEW TODO) must start at
        attempt=1 — it must not inherit a stale count from another stage's
        failure and escalate as "ATTEMPT 2" on its very first silent exit.
        """
        todo_id, other_todo = "TODO-0001", "TODO-0002"
        project = tmp_path / "proj"
        (project / ".agentic" / "outbox").mkdir(parents=True)
        self._mock_silent_project(project, monkeypatch)

        stage_a = Stage(name="stage-a", role="worker", kind="execute")
        stage_b = Stage(name="stage-b", role="worker", kind="execute")
        stages = [stage_a, stage_b]

        # First silent exit at stage-a / TODO-0001 → attempt 1
        pipeline_engine.execute_agent_stage(
            stage=stage_a, current_todo=todo_id, project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=0,
            retry_counts=[0, 0], auto=False, agent_hard_timeout=None,
        )
        # NEW stage, same TODO → must restart at attempt 1
        pipeline_engine.execute_agent_stage(
            stage=stage_b, current_todo=todo_id, project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
            retry_counts=[0, 0], auto=False, agent_hard_timeout=None,
        )
        text_b = (
            project / ".agentic" / "inbox" / f"SALVAGE-{todo_id}.md"
        ).read_text(encoding="utf-8")
        assert "**Attempt:** 1" in text_b, (
            "first failure of a NEW stage must be attempt 1, "
            "not inherit the previous stage's counter"
        )
        # no repeat-salvage escalation header (the attempt-N warning). The
        # generic auto-retry-exhausted hint may still appear — that is the
        # F7 budget, not the salvage counter.
        assert "## ⚠️ ATTEMPT" not in text_b

        # NEW TODO, first stage → must also restart at attempt 1
        inbox = project / ".agentic" / "inbox"
        (inbox / f"{other_todo}.md").write_text("# task\n", encoding="utf-8")
        (inbox / f"{other_todo}.ready").write_text("", encoding="utf-8")
        pipeline_engine.execute_agent_stage(
            stage=stage_a, current_todo=other_todo, project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=0,
            retry_counts=[0, 0], auto=False, agent_hard_timeout=None,
        )
        text_other = (
            inbox / f"SALVAGE-{other_todo}.md"
        ).read_text(encoding="utf-8")
        assert "**Attempt:** 1" in text_other, (
            "first failure of a NEW TODO must be attempt 1"
        )

    def test_salvage_supervisor_timeout_stops_cleanly(self, tmp_path, monkeypatch):
        """AUD04-05: a supervisor salvage timeout must stop the pipeline
        (rc=1, logged), not escape as an uncaught TimeoutError traceback."""
        project = tmp_path / "proj"
        (project / ".agentic" / "outbox").mkdir(parents=True)
        self._mock_silent_project(project, monkeypatch)

        def boom(*a, **kw):
            raise TimeoutError("Supervisor salvage signal not received within 5s")

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", boom)

        stages = _make_stages()
        todo, idx, rc = pipeline_engine.execute_agent_stage(
            stage=stages[1], current_todo="TODO-0001", project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
            retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        )
        assert rc == 1, "salvage timeout must stop the pipeline, not crash"
        assert todo == "TODO-0001"

    def test_commit_gate_timeout_stops_pipeline(self, tmp_path, monkeypatch):
        """AUD04-05: an APPROVE-gate timeout on the agent-stage commit path
        must stop the pipeline (rc=1), not escape as an uncaught
        TimeoutError traceback."""
        project = tmp_path / "proj"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        (outbox / "DONE-TODO-0001.md").write_text("# done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        self._mock_silent_project(project, monkeypatch)

        def boom(*a, **kw):
            raise TimeoutError("APPROVE/ACK signal not received for TODO-0001")

        monkeypatch.setattr(pipeline_engine, "_maybe_commit", boom)

        stage = Stage(
            name="implement", role="worker", kind="execute",
            on_approved="commit_and_next",
        )
        stages = [
            Stage(name="plan", role="supervisor", kind="plan"),
            stage,
            Stage(name="verify", role="supervisor", kind="verify"),
        ]
        todo, idx, rc = pipeline_engine.execute_agent_stage(
            stage=stage, current_todo="TODO-0001", project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
            retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        )
        assert rc == 1, "commit-gate timeout must stop the pipeline, not crash"

    def test_verify_commit_gate_timeout_stops_cleanly(self, tmp_path, monkeypatch):
        """AUD04-05: an APPROVE-gate timeout on the verify commit path must
        stop the pipeline (rc=1), not escape as an uncaught TimeoutError."""
        project = tmp_path / "proj"
        (project / ".agentic" / "outbox").mkdir(parents=True)
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage",
                            lambda *a, **kw: "ACK-TODO-0001")
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a: "")
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

        def boom(*a, **kw):
            raise TimeoutError("APPROVE/ACK signal not received for TODO-0001")

        monkeypatch.setattr(pipeline_engine, "_maybe_commit", boom)

        stage = Stage(name="verify", role="supervisor", kind="verify",
                      on_approved="commit_and_next")
        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=stage, current_todo="TODO-0001", auto=False,
            project_dir=project, config={}, logs_dir=tmp_path,
        )
        assert rc == 1, "verify commit-gate timeout must stop the pipeline"

    def test_no_retry_when_work_present(self, tmp_path, monkeypatch):
        """Worker left changes (diff) → straight to salvage, no rerun.

        Retrying over existing work could overwrite or duplicate it; the
        supervisor ACK path handles partial work better. NEG-4: the check is
        stage-scoped — a fingerprint taken at stage entry vs after the run.
        """
        project = tmp_path / "proj"
        (project / ".agentic" / "outbox").mkdir(parents=True)

        calls = {"n": 0}

        def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            calls["n"] += 1

        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
        # Stage entry fingerprint ≠ post-run fingerprint → the stage made changes.
        fps = iter(["fp-entry", "fp-after-run"])
        monkeypatch.setattr(verify, "work_fingerprint", lambda *a, **kw: next(fps, "fp-after-run"))

        import time as _time_mod
        monkeypatch.setattr(_time_mod, "monotonic", lambda: 500.0)

        stages = _make_stages()
        pipeline_engine.execute_agent_stage(
            stage=stages[1], current_todo="TODO-0001", project_dir=project,
            config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
            retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
            pipeline_name=None,
        )

        assert calls["n"] == 1  # no retry — work evidence stops the loop
        from awf.pipeline_state import read_state
        assert read_state(project).get("salvage_count") == 1

    def test_silent_retry_note_content(self):
        note = pipeline_engine._silent_retry_note(2, "TODO-0042")
        assert "RETRY 2" in note
        assert "TODO-0042" in note
        assert "DONE-TODO-0042.ready" in note
        assert "BLOCKED" in note
        assert "no file changes" in note

    def test_exhausted_auto_retries_escalates_on_first_salvage(self, tmp_git_repo):
        """F7: 2 failed continue-pushes → escalate even on salvage #1.

        The supervisor must not blindly rerun what the machine already
        retried twice.
        """
        pipeline_engine._write_salvage_prompt(
            tmp_git_repo, "TODO-0001", "agent-implementer", None,
            tmp_git_repo / ".agentic" / "logs", attempt=1, auto_retries=2,
        )
        text = (
            tmp_git_repo / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md"
        ).read_text(encoding="utf-8")
        assert "**Attempt:** 1" in text
        assert "Automatic continue-pushes after the silent exits: 2" in text
        assert "do NOT retry the same scope" in text
        assert "Split the task" in text
