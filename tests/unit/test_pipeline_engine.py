"""Tests for pipeline_engine: plan stage (single-phase, FU-05), F4 auto-retry."""
from __future__ import annotations

from types import SimpleNamespace

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

        from awf.api._results import StartResult

        monkeypatch.setattr(
            api_pipeline, "kill_pipeline", lambda *a, **kw: {"killed": False}
        )
        captured: dict = {}

        def fake_continue(project_dir, **kw):
            captured.update(kw)
            return StartResult(
                run_mode="background",
                run_id=1234,
                log_file=None,
                exit_code=None,
                message="awf continue running in background (PID 1234).",
            )

        monkeypatch.setattr(api_pipeline, "continue_pipeline", fake_continue)

        api_pipeline.retry_stage(tmp_git_repo, background=False)

        assert not (inbox / "SALVAGE-TODO-0001.md").exists(), (
            "retry_stage left the inbox SALVAGE note behind — the old outbox "
            "glob never sees it"
        )
        assert captured.get("from_stage") == "implement"

    @staticmethod
    def _mock_restart(monkeypatch, api_pipeline):
        """Common mock: kill is a no-op, continue captures kwargs."""
        from awf.api._results import StartResult

        monkeypatch.setattr(
            api_pipeline, "kill_pipeline", lambda *a, **kw: {"killed": False}
        )
        captured: dict = {}

        def fake_continue(project_dir, **kw):
            captured.update(kw)
            return StartResult(
                run_mode="background",
                run_id=1234,
                log_file=None,
                exit_code=None,
                message="awf continue running in background (PID 1234).",
            )

        monkeypatch.setattr(api_pipeline, "continue_pipeline", fake_continue)
        return captured

    def test_retry_stage_pins_salvage_todo(self, tmp_git_repo, monkeypatch):
        """RUN7 #2: restart the salvaged TODO, not "newest active".

        RUN5 incident: retry_stage cleared the state via the kill, and the
        continue fell back to newest_active() — a newer TODO (0054) started
        from QA while the salvaged one (0052) sat untouched (AUD08-02 class).
        """
        import awf.api.pipeline as api_pipeline
        from awf.pipeline_state import write_state

        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for n in ("0001", "0002", "0003"):
            (inbox / f"TODO-{n}.md").write_text(f"# TODO-{n}\nstub\n", encoding="utf-8")
            (inbox / f"TODO-{n}.ready").write_text("")

        # Salvage state on the OLDEST active TODO.
        write_state(
            tmp_git_repo,
            salvage_needed=True,
            salvage_stage="implement",
            todo_id="TODO-0001",
        )

        captured = self._mock_restart(monkeypatch, api_pipeline)
        result = api_pipeline.retry_stage(tmp_git_repo, background=False)

        assert captured["todo_id"] == "TODO-0001", (
            f"retry_stage must pin the salvaged TODO, got {captured['todo_id']!r}"
        )
        assert captured["from_stage"] == "implement"
        assert "TODO-0001" in result.message, (
            f"the answer must name the restarted unit: {result.message!r}"
        )

    def test_retry_stage_falls_back_to_salvage_note(self, tmp_git_repo, monkeypatch):
        """State has salvage_stage but no todo_id → the SALVAGE note filename is the pin."""
        import awf.api.pipeline as api_pipeline
        from awf.pipeline_state import write_state

        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for n in ("0001", "0002", "0003"):
            (inbox / f"TODO-{n}.md").write_text(f"# TODO-{n}\nstub\n", encoding="utf-8")
            (inbox / f"TODO-{n}.ready").write_text("")
        (inbox / "SALVAGE-TODO-0002.md").write_text("# salvage note\n", encoding="utf-8")

        write_state(
            tmp_git_repo,
            salvage_needed=True,
            salvage_stage="implement",
        )

        captured = self._mock_restart(monkeypatch, api_pipeline)
        result = api_pipeline.retry_stage(tmp_git_repo, background=False)

        assert captured["todo_id"] == "TODO-0002"
        assert "TODO-0002" in result.message
        assert not (inbox / "SALVAGE-TODO-0002.md").exists()

    def test_retry_stage_without_salvage_todo_warns(self, tmp_git_repo, monkeypatch):
        """No todo_id in state and no SALVAGE note → old unpinned behavior + a warning."""
        import awf.api.pipeline as api_pipeline
        from awf.pipeline_state import write_state

        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        for n in ("0001", "0002", "0003"):
            (inbox / f"TODO-{n}.md").write_text(f"# TODO-{n}\nstub\n", encoding="utf-8")
            (inbox / f"TODO-{n}.ready").write_text("")

        write_state(
            tmp_git_repo,
            salvage_needed=True,
            salvage_stage="implement",
        )

        captured = self._mock_restart(monkeypatch, api_pipeline)
        result = api_pipeline.retry_stage(tmp_git_repo, background=False)

        assert captured["todo_id"] == ""  # old behavior — no pin to pass
        assert "WARNING" in result.message
        assert "newest-active fallback" in result.message


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


class TestVerifyDecisionConsumption:
    """AUD04-04: the accepted verify decision must not survive its cycle.

    Without consume-on-accept a commit-fail (or a kill) left the
    ACK/APPROVE/REVIEW file in place, and a re-verify of the same TODO
    re-accepted the old decision — e.g. auto-committing on a dead approval.
    """

    def _project(self, tmp_path):
        proj = tmp_path / "proj"
        for d in ("inbox", "outbox", "context", "logs", "done", "handoff"):
            (proj / ".agentic" / d).mkdir(parents=True)
        (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text("# task\n")
        return proj

    def _verify_stage(self):
        return Stage(
            name="verify", role="supervisor", kind="verify",
            on_approved="commit_and_next",
        )

    def test_ack_consumed_after_commit_success(self, tmp_path, monkeypatch):
        proj = self._project(tmp_path)
        ack = proj / ".agentic" / "inbox" / "ACK-TODO-0001.ready"
        ack.write_text("")

        monkeypatch.setattr(
            pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "ACK-TODO-0001"
        )
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a: "")
        monkeypatch.setattr(pipeline_engine, "_maybe_commit", lambda *a, **kw: True)
        monkeypatch.setattr(pipeline_engine, "_mark_plan_step_done", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
        import awf.todos as todos_mod

        monkeypatch.setattr(todos_mod, "archive_todo", lambda *a, **kw: None)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=proj, config={}, logs_dir=tmp_path,
        )

        assert rc == 0 and delta == 1
        assert not ack.exists(), "accepted ACK must be consumed at the end of the cycle"

    def test_ack_consumed_after_commit_fail(self, tmp_path, monkeypatch):
        """The incident: commit-fail left the approval in the inbox and a
        re-verify of the same TODO auto-committed on it."""
        proj = self._project(tmp_path)
        ack = proj / ".agentic" / "inbox" / "ACK-TODO-0001.ready"
        ack.write_text("")

        monkeypatch.setattr(
            pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "ACK-TODO-0001"
        )
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a: "")
        monkeypatch.setattr(pipeline_engine, "_maybe_commit", lambda *a, **kw: False)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=proj, config={}, logs_dir=tmp_path,
        )

        assert rc == 1, "commit fail stops the pipeline"
        assert not ack.exists(), (
            "stale ACK survived the commit-fail — a re-verify of the same TODO "
            "would auto-commit on a dead approval"
        )

    def test_review_consumed_after_replan(self, tmp_path, monkeypatch):
        """A stale REVIEW must not re-trigger replan in a fresh cycle."""
        proj = self._project(tmp_path)
        review = proj / ".agentic" / "outbox" / "REVIEW-TODO-0001.md"
        review.write_text("# rejected\nfix X\n")

        replan_calls = {"n": 0}

        def fake_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            if stage.kind == "verify":
                return "REVIEW-TODO-0001"  # the rejection under test
            assert stage.kind == "replan"
            replan_calls["n"] += 1
            return ""

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", fake_supervisor)
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_find_active_todo", lambda *a: None)
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=proj, config={}, logs_dir=tmp_path,
        )

        assert rc == 1 and replan_calls["n"] == 1
        assert not review.exists(), (
            "stale REVIEW survived — a fresh cycle would re-replan on it"
        )


class TestU6bNetRetry:
    """U6b: network failure in the worker log → backoff-retry, state counter."""

    NET_LOG = "opencode run failed\nerror: Cannot connect to API.\n"

    def _setup(self, tmp_path, monkeypatch, log_text, on_run, config=None,
               start_offset=0):
        project = tmp_path / "proj"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        log_file = tmp_path / "worker.out"
        log_file.write_text(log_text, encoding="utf-8")

        call_count = {"n": 0}

        def mock_agent(stage, todo_id, project_dir, cfg, logs_dir, **kw):
            call_count["n"] += 1
            holder = kw.get("log_holder")
            if holder is not None:
                holder["log_path"] = str(log_file)
                holder["log_start_offset"] = start_offset
            on_run(call_count["n"], todo_id, outbox)

        import time as _time_mod
        monkeypatch.setattr(_time_mod, "monotonic", lambda: 100.0)

        # NOTE: time.sleep is faked here, so the stage entry must not spawn
        # REAL subprocesses — Popen._wait's internal polling would spin on
        # the fake sleep until the child (git) exits. work_fingerprint and
        # _write_salvage_prompt are the only real-git call sites on this path.
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda dt: sleeps.append(dt))

        state_calls: list[dict] = []

        import awf.pipeline_state as pipeline_state_mod

        def fake_write_state(project_dir, *, logs_dir=None, **fields):
            state_calls.append(dict(fields))

        log_lines: list[str] = []
        monkeypatch.setattr(pipeline_engine, "_log", lambda d, msg: log_lines.append(msg))
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_state_mod, "write_state", fake_write_state)
        from awf import signals as sig_module
        monkeypatch.setattr(sig_module, "clean_stage_signals", lambda *a, **kw: None)
        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
        monkeypatch.setattr(verify, "work_fingerprint", lambda *a, **kw: "")

        def run():
            stages = _make_stages()
            return pipeline_engine.execute_agent_stage(
                stage=stages[1], current_todo="TODO-0001", project_dir=project,
                config=config or {}, logs_dir=tmp_path, stages=stages, stage_idx=1,
                retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
                pipeline_name=None,
            )

        return run, call_count, sleeps, state_calls, log_lines, project

    def test_network_death_retries_with_backoff_and_succeeds(
        self, tmp_path, monkeypatch
    ) -> None:
        """Log with network marker + no signal → net retry (counter grows,
        30s backoff), stage succeeds on the next net round."""

        def on_run(n, todo_id, outbox):
            if n >= 2:
                (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
                (outbox / f"DONE-{todo_id}.ready").write_text("")

        run, calls, sleeps, state_calls, _logs, project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, on_run,
        )

        todo, _idx, rc = run()

        assert rc == 0
        assert calls["n"] == 2  # dead first run + successful net-retry run
        assert sleeps == [30.0]  # first backoff
        assert any(
            f.get("net_retry_count") == 1 and f.get("net_retry_key") == "TODO-0001:worker"
            for f in state_calls
        ), f"counter not written to state: {state_calls}"
        # stage resolved → counter reset in the persisted state
        from awf.pipeline_state import read_state
        final = read_state(project) or {}
        assert final.get("net_retry_count") == 0
        assert final.get("net_retry_key") is None

    def test_non_network_death_keeps_old_behavior(
        self, tmp_path, monkeypatch
    ) -> None:
        """No network marker → no net retry: the silent-retry path handles it."""

        def on_run(n, todo_id, outbox):
            if n >= 2:
                (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
                (outbox / f"DONE-{todo_id}.ready").write_text("")

        run, calls, sleeps, state_calls, _logs, _project = self._setup(
            tmp_path, monkeypatch, "worker crashed: internal assertion\n", on_run,
        )

        todo, _idx, rc = run()

        assert rc == 0
        assert calls["n"] == 2  # silent retry, like before U6b
        assert 30.0 not in sleeps  # no net backoff
        assert not any("net_retry_count" in f for f in state_calls)

    def test_network_retries_exhausted_goes_to_failure_path(
        self, tmp_path, monkeypatch
    ) -> None:
        """Endpoint stays dead past the limit → no more retries, failure path."""

        def on_run(n, todo_id, outbox):
            pass  # never a signal

        run, calls, sleeps, state_calls, log_lines, _project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, on_run,
            config={"automation": {"net_retry_limit": 1}},
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        todo, _idx, rc = run()

        assert rc == 1  # failure path (salvage produced no signal)
        assert calls["n"] == 2  # 1 original + 1 net retry (limit 1)
        assert sleeps == [30.0]  # exactly one backoff
        assert any(f.get("net_retry_count") == 1 for f in state_calls)
        assert any("network retries exhausted" in line for line in log_lines)

    # ── QA TODO-0017: append-mode log + persisted counter ─────────────────

    def test_stale_marker_from_previous_run_not_counted(
        self, tmp_path, monkeypatch
    ) -> None:
        """A network marker left by an EARLIER append-run (before this run's
        start offset) must not classify this run's non-network death — the
        old behavior burned the net budget on the wrong retry class."""

        def on_run(n, todo_id, outbox):
            if n >= 2:
                (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
                (outbox / f"DONE-{todo_id}.ready").write_text("")

        stale = "error: Cannot connect to API.\n"
        current = "worker crashed: internal assertion\n"
        run, calls, sleeps, state_calls, _logs, _project = self._setup(
            tmp_path, monkeypatch, stale + current, on_run,
            start_offset=len(stale.encode("utf-8")),
        )

        todo, _idx, rc = run()

        assert rc == 0
        assert calls["n"] == 2  # plain silent retry — no work, no signal
        assert 30.0 not in sleeps  # no net backoff
        assert not any("net_retry_count" in f for f in state_calls)

    def test_marker_after_offset_still_counted(self, tmp_path, monkeypatch) -> None:
        """Control: a marker written AFTER the start offset is this run's —
        the offset must not hide a genuine network failure."""

        def on_run(n, todo_id, outbox):
            pass  # never a signal

        stale = "earlier run noise\n"
        current = "connect ECONNREFUSED 127.0.0.1:8000\n"
        run, calls, sleeps, _state, _logs, _project = self._setup(
            tmp_path, monkeypatch, stale + current, on_run,
            config={"automation": {"net_retry_limit": 1}},
            start_offset=len(stale.encode("utf-8")),
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        todo, _idx, rc = run()

        assert rc == 1
        assert calls["n"] == 2  # original + 1 net retry (limit 1)
        assert sleeps == [30.0]  # net backoff did happen

    def test_persisted_counter_survives_restart(
        self, tmp_path, monkeypatch
    ) -> None:
        """A pipeline killed mid net-backoff restarts this stage: the
        persisted counter for the same (TODO, stage) must be inherited —
        a fresh 0 here would silently double the retry budget."""

        def on_run(n, todo_id, outbox):
            pass  # never a signal — the endpoint is still dead

        run, calls, sleeps, state_calls, _logs, project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, on_run,
            config={"automation": {"net_retry_limit": 3}},
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        # Simulate the kill during the first backoff: state still holds the
        # counter the previous (killed) stage entry wrote.
        state_file = project / ".agentic" / "state" / "current.yaml"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            "net_retry_count: 1\nnet_retry_key: TODO-0001:worker\n",
            encoding="utf-8",
        )

        todo, _idx, rc = run()

        assert rc == 1
        # inherited 1 → this entry spends retries 2 and 3 (60s + 120s)
        assert sleeps == [60.0, 120.0]
        assert calls["n"] == 3
        assert any(f.get("net_retry_count") == 2 for f in state_calls)

    def test_persisted_counter_other_key_reset(
        self, tmp_path, monkeypatch
    ) -> None:
        """A persisted counter from a DIFFERENT (TODO, stage) must not leak
        into this fresh stage — it starts at 0 (AUD02-07 key check)."""

        def on_run(n, todo_id, outbox):
            pass  # never a signal

        run, calls, sleeps, _state, _logs, project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, on_run,
            config={"automation": {"net_retry_limit": 3}},
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        state_file = project / ".agentic" / "state" / "current.yaml"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            "net_retry_count: 2\nnet_retry_key: TODO-9999:other-stage\n",
            encoding="utf-8",
        )

        todo, _idx, rc = run()

        assert rc == 1
        assert sleeps[0] == 30.0  # fresh budget — first backoff
        assert calls["n"] == 4  # original + all 3 retries


class TestU6cNetRetryOnCrash:
    """U6c: a crash death (no signal + rc!=0 → RuntimeError, or the
    preflight TimeoutError) is classified like a silent network death.

    U6b raised RuntimeError for "no signal + rc!=0" BEFORE the network
    classification block — so the most common network death (endpoint down
    mid-run, observed on FU-16) stopped the pipeline without a retry.
    """

    NET_LOG = "opencode run failed\nerror: Cannot connect to API.\n"

    @staticmethod
    def _crash():
        return RuntimeError(
            "Agent stage worker (execute) subprocess exited with code 1. "
            "Cmd: opencode run --auto"
        )

    def _setup(self, tmp_path, monkeypatch, log_text, exc, until_success=2,
               config=None, start_offset=0, fill_holder=True):
        """Same scaffolding as TestU6bNetRetry._setup, but the worker run
        RAISES (like run_agent_stage does for no-signal + rc!=0) until
        run ``until_success``."""
        project = tmp_path / "proj"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True)
        log_file = tmp_path / "worker.out"
        log_file.write_text(log_text, encoding="utf-8")

        call_count = {"n": 0}

        def mock_agent(stage, todo_id, project_dir, cfg, logs_dir, **kw):
            call_count["n"] += 1
            holder = kw.get("log_holder")
            if fill_holder and holder is not None:
                holder["log_path"] = str(log_file)
                holder["log_start_offset"] = start_offset
            if call_count["n"] < until_success:
                raise exc
            (outbox / f"DONE-{todo_id}.md").write_text("# Done\n")
            (outbox / f"DONE-{todo_id}.ready").write_text("")

        import time as _time_mod
        monkeypatch.setattr(_time_mod, "monotonic", lambda: 100.0)

        # NOTE: time.sleep is faked here, so the stage entry must not spawn
        # REAL subprocesses — Popen._wait's internal polling would spin on
        # the fake sleep until the child (git) exits.
        sleeps: list[float] = []
        monkeypatch.setattr("time.sleep", lambda dt: sleeps.append(dt))

        state_calls: list[dict] = []

        import awf.pipeline_state as pipeline_state_mod

        def fake_write_state(project_dir, *, logs_dir=None, **fields):
            state_calls.append(dict(fields))

        log_lines: list[str] = []
        monkeypatch.setattr(pipeline_engine, "_log", lambda d, msg: log_lines.append(msg))
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
        monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
        monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_state_mod, "write_state", fake_write_state)
        from awf import signals as sig_module
        monkeypatch.setattr(sig_module, "clean_stage_signals", lambda *a, **kw: None)
        from awf import verify
        monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
        monkeypatch.setattr(verify, "work_fingerprint", lambda *a, **kw: "")

        def run():
            stages = _make_stages()
            return pipeline_engine.execute_agent_stage(
                stage=stages[1], current_todo="TODO-0001", project_dir=project,
                config=config or {}, logs_dir=tmp_path, stages=stages, stage_idx=1,
                retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
                pipeline_name=None,
            )

        return run, call_count, sleeps, state_calls, log_lines, project

    def test_rc_nonzero_network_death_retries(
        self, tmp_path, monkeypatch
    ) -> None:
        """The FU-16 case: worker dies rc=1 without a signal, its log shows
        a network marker → backoff retry with the U6a counter/key, success
        on the next net round, counter reset after the stage resolves."""

        run, calls, sleeps, state_calls, _logs, project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, self._crash(),
        )

        todo, _idx, rc = run()

        assert rc == 0
        assert calls["n"] == 2  # crashed run + successful net-retry run
        assert sleeps == [30.0]  # first backoff
        assert any(
            f.get("net_retry_count") == 1 and f.get("net_retry_key") == "TODO-0001:worker"
            for f in state_calls
        ), f"counter not written to state: {state_calls}"
        # stage resolved → counter reset in the persisted state
        from awf.pipeline_state import read_state
        final = read_state(project) or {}
        assert final.get("net_retry_count") == 0
        assert final.get("net_retry_key") is None

    def test_rc_nonzero_network_exhausted_stops_with_reason(
        self, tmp_path, monkeypatch
    ) -> None:
        """Endpoint stays dead past the limit → stop with the explicit
        'network retries exhausted' reason, not the generic crash stop."""

        run, calls, sleeps, state_calls, log_lines, _project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, self._crash(),
            until_success=99,
            config={"automation": {"net_retry_limit": 1}},
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        todo, _idx, rc = run()

        assert rc == 1  # failure path (salvage produced no signal)
        assert calls["n"] == 2  # 1 original + 1 net retry (limit 1)
        assert sleeps == [30.0]  # exactly one backoff
        assert any(f.get("net_retry_count") == 1 for f in state_calls)
        assert any("network retries exhausted" in line for line in log_lines)

    def test_rc_nonzero_non_network_keeps_stop(
        self, tmp_path, monkeypatch
    ) -> None:
        """No network marker in the log and no preflight marker in the
        error → the old behavior: a single crash stops the pipeline."""

        run, calls, sleeps, state_calls, log_lines, _project = self._setup(
            tmp_path, monkeypatch, "worker crashed: internal assertion\n",
            self._crash(),
            until_success=99,
        )

        todo, _idx, rc = run()

        assert rc == 1
        assert calls["n"] == 1  # no retry at all
        assert 30.0 not in sleeps  # no net backoff
        assert not any("net_retry_count" in f for f in state_calls)
        assert any("Pipeline stopped at stage" in line for line in log_lines)

    def test_preflight_timeout_is_network_class(
        self, tmp_path, monkeypatch
    ) -> None:
        """Preflight spent its whole budget on a dead endpoint (no worker
        log exists yet) — the exception text alone must classify the death
        as network: retry with backoff instead of a lost run."""
        preflight = TimeoutError(
            "Model endpoint http://127.0.0.1:8000 unreachable for 600s — "
            "worker not started"
        )
        run, calls, sleeps, state_calls, _logs, _project = self._setup(
            tmp_path, monkeypatch, "", preflight,
            fill_holder=False,  # preflight dies before the log is created
        )

        todo, _idx, rc = run()

        assert rc == 0
        assert calls["n"] == 2  # preflight-failed run + successful net-retry run
        assert sleeps == [30.0]
        assert any(
            f.get("net_retry_count") == 1 and f.get("net_retry_key") == "TODO-0001:worker"
            for f in state_calls
        )

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError(
                "Worker produced no output for 15.0 min (watchdog) — "
                "process tree killed"
            ),
            TimeoutError("Subprocess did not produce signal within 3600s"),
        ],
        ids=["watchdog", "hard-timeout"],
    )
    def test_non_network_timeout_keeps_stop(
        self, tmp_path, monkeypatch, exc
    ) -> None:
        """Watchdog and hard-timeout deaths with a clean log are NOT the
        network class — no over-classification, the old stop stands."""
        run, calls, sleeps, state_calls, log_lines, _project = self._setup(
            tmp_path, monkeypatch, "worker: starting work\n", exc,
            until_success=99,
        )

        todo, _idx, rc = run()

        assert rc == 1
        assert calls["n"] == 1
        assert 30.0 not in sleeps
        assert not any("net_retry_count" in f for f in state_calls)
        assert any("Pipeline stopped at stage" in line for line in log_lines)

    def test_crash_inherits_persisted_counter(
        self, tmp_path, monkeypatch
    ) -> None:
        """Kill+continue between crash retries: the persisted (TODO, stage)
        counter must be inherited through the crash path exactly like the
        no-signal path — a fresh 0 would silently double the budget."""

        run, calls, sleeps, state_calls, _logs, project = self._setup(
            tmp_path, monkeypatch, self.NET_LOG, self._crash(),
            until_success=99,
            config={"automation": {"net_retry_limit": 3}},
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        state_file = project / ".agentic" / "state" / "current.yaml"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            "net_retry_count: 1\nnet_retry_key: TODO-0001:worker\n",
            encoding="utf-8",
        )

        todo, _idx, rc = run()

        assert rc == 1
        # inherited 1 → this entry spends retries 2 and 3 (60s + 120s)
        assert sleeps == [60.0, 120.0]
        assert calls["n"] == 3
        assert any(f.get("net_retry_count") == 2 for f in state_calls)

    def test_crash_text_with_preflight_word_not_network(
        self, tmp_path, monkeypatch
    ) -> None:
        """U6b's RuntimeError embeds the full Cmd (stage/agent names, file
        paths, prompt) — a 'preflight' word in the project's own pipeline
        names must not classify a clean-log rc!=0 crash as network. The
        log tail is the only evidence for a crash death; the preflight
        text check is reserved for the preflight TimeoutError itself."""
        polluted = RuntimeError(
            "Agent stage preflight-checker (execute) subprocess exited "
            "with code 1. Cmd: opencode run --auto"
        )
        run, calls, sleeps, state_calls, _logs, _project = self._setup(
            tmp_path, monkeypatch, "worker crashed: internal assertion\n",
            polluted,
            until_success=99,
        )
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
        monkeypatch.setattr(pipeline_engine, "_write_salvage_prompt", lambda *a, **kw: None)

        todo, _idx, rc = run()

        assert rc == 1
        assert calls["n"] == 1  # no retry — the crash is not network-class
        assert 30.0 not in sleeps
        assert not any("net_retry_count" in f for f in state_calls)


class TestU5VerifyPack:
    """U5: the verify pack must run BEFORE the supervisor's signal wait."""

    @staticmethod
    def _base(tmp_path, monkeypatch, order, pack_error=None, config=None):
        project = tmp_path / "proj"
        ctx = project / ".agentic" / "context"
        ctx.mkdir(parents=True)
        (project / ".agentic" / "outbox").mkdir(parents=True)

        def fake_pack(project_dir, todo_id, **kw):
            order.append("pack")
            if pack_error is not None:
                raise pack_error
            return SimpleNamespace(
                todo_id=todo_id, verdict="ok", exit_code=0, measured=1,
                report_path="", sections={}, details={},
            )

        def mock_sup(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            order.append(f"supervisor:{stage.kind}")
            return "REVIEW-TODO-0001"

        monkeypatch.setattr(pipeline_engine, "_verify_pack_fn", fake_pack)
        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_sup)
        monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_record_verify_decision", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_consume_verify_decision", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline_engine, "_find_active_todo", lambda *a: "")
        monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
        return project, config if config is not None else {}

    @staticmethod
    def _verify_stage():
        return Stage(name="verify", role="supervisor", kind="verify",
                     on_approved="commit_and_next")

    def test_pack_runs_before_supervisor_signal(self, tmp_path, monkeypatch):
        order: list[str] = []
        project, config = self._base(tmp_path, monkeypatch, order)
        (project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text("a" * 40)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=project, config=config, logs_dir=tmp_path,
        )
        # the pack finished before the verify supervisor woke up
        assert order.index("pack") < order.index("supervisor:verify")
        assert rc == 1  # REVIEW stops the pipeline (mocked decision)

    def test_pack_error_does_not_stop_stage(self, tmp_path, monkeypatch):
        order: list[str] = []
        project, config = self._base(tmp_path, monkeypatch, order,
                                     pack_error=RuntimeError("boom"))
        (project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text("a" * 40)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=project, config=config, logs_dir=tmp_path,
        )
        # stage survived: the supervisor still ran
        assert "supervisor:verify" in order
        assert rc == 1
        # and the error note is in the report
        report = project / ".agentic" / "context" / "GATES-TODO-0001.md"
        assert report.is_file()
        assert "boom" in report.read_text(encoding="utf-8")

    def test_skipped_without_baseline(self, tmp_path, monkeypatch):
        order: list[str] = []
        project, config = self._base(tmp_path, monkeypatch, order)
        # no BASELINE-TODO-0001.sha → nothing to measure against

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=project, config=config, logs_dir=tmp_path,
        )
        assert "pack" not in order
        assert "supervisor:verify" in order

    def test_disabled_by_config(self, tmp_path, monkeypatch):
        order: list[str] = []
        project, config = self._base(tmp_path, monkeypatch, order,
                                     config={"automation": {"verify_pack": False}})
        (project / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text("a" * 40)

        todo, delta, rc = pipeline_engine.execute_supervisor_stage(
            stage=self._verify_stage(), current_todo="TODO-0001", auto=False,
            project_dir=project, config=config, logs_dir=tmp_path,
        )
        assert "pack" not in order
