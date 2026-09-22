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


class TestDoneCycleDetection:
    """RUN6 #1: after approve the pipeline commits, archives and exits —
    the wake-up must answer 'done' with the next step, not a full timeout
    on a null state (owner report 22.09, topic-trainer, 5/5 repeats).

    The orchestrator's clean exit CLEARS the stage markers but rewrites
    the state file with phase=done + goal/normalized (AUD02-04) — the file
    exists, yet no pipeline is running. That leftover must be treated as
    'pipeline exited', not as 'still running, keep waiting'.
    """

    @staticmethod
    def _simulate_clean_exit(
        project,
        todo_id: str = "TODO-0001",
        *,
        with_commit: bool = True,
        with_archive: bool = True,
    ) -> None:
        """Mirror the orchestrator's clean exit: the cycle signs (verify
        commit + done/<id>/ archive) are in place, then the stage state is
        cleared and the phase=done leftover rewritten (orchestrator.py)."""
        import subprocess

        from awf.pipeline_state import clear_state

        write_state(
            project,
            stage_name="verify",
            stage_kind="verify",
            stage_idx=3,
            todo_id=todo_id,
            pipeline_pid=99999,
        )
        if with_commit:
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", f"awf(verify): {todo_id}"],
                cwd=project,
                check=True,
            )
        if with_archive:
            d = project / ".agentic" / "done" / todo_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")
        clear_state(project)
        write_state(project, phase="done", goal="test goal", normalized=True)

    def test_done_immediately_after_clean_exit(self, awf_project):
        """State markers cleared + process dead + cycle signs present →
        'done' on the FIRST call, in seconds (no poll-loop sleep)."""
        import time as _time

        self._simulate_clean_exit(awf_project)
        t0 = _time.monotonic()
        result = api.wait_for_event(awf_project, timeout=5, poll_interval=1)
        elapsed = _time.monotonic() - t0

        assert result.event_type == "done"
        assert "TODO-0001" in result.message
        assert elapsed < 1.0, "done must come from the initial check, not the poll loop"

    def test_done_message_next_step_run_next_when_run_active(self, awf_project):
        """Inside a run (забег) the message leads to awf_run_next."""
        from awf.run_state import write_run

        self._simulate_clean_exit(awf_project)
        write_run(
            awf_project,
            queue=["TODO-0001", "TODO-0002"],
            index=1,
            active=True,
        )
        result = api.wait_for_event(awf_project, timeout=2)

        assert result.event_type == "done"
        assert "TODO-0001" in result.message
        assert "awf_run_next" in result.message
        assert "awf_dispatch_todo" not in result.message

    def test_done_message_next_step_dispatch_without_run(self, awf_project):
        """Single start (no run): the message leads to dispatch, not run_next."""
        self._simulate_clean_exit(awf_project)
        result = api.wait_for_event(awf_project, timeout=2)

        assert result.event_type == "done"
        assert "TODO-0001" in result.message
        assert "awf_dispatch_todo" in result.message
        assert "awf_run_next" not in result.message

    def test_not_done_when_pipeline_alive(self, awf_project, monkeypatch):
        """A LIVE pipeline (cleared state or not) never gets a false 'done'."""
        self._simulate_clean_exit(awf_project)
        monkeypatch.setattr(
            "awf.api._liveness.resolve", lambda pd: (True, 4242, "state")
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)

        assert result.event_type == "timeout", (
            f"live pipeline must keep the old wait behavior, got {result.event_type}"
        )
        # and with markers still present — also no done
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
            stage_idx=1,
            todo_id="TODO-0002",
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)
        assert result.event_type == "timeout"

    def test_not_done_when_state_still_has_markers(self, awf_project):
        """Non-empty stage state (pipeline mid-run) → old behavior, even
        when an OLDER cycle's signs are present."""
        d = awf_project / ".agentic" / "done" / "TODO-0001"
        d.mkdir(parents=True, exist_ok=True)
        (d / "TODO.md").write_text("# TODO-0001\n", encoding="utf-8")
        write_state(
            awf_project,
            stage_name="developer",
            stage_kind="execute",
            stage_idx=1,
            todo_id="TODO-0002",
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)
        assert result.event_type == "timeout"

    def test_done_when_state_file_absent(self, awf_project):
        """No state file at all + cycle signs → 'done' (the file-missing
        variant of the same detection)."""
        import subprocess

        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", "awf(verify): TODO-0001"],
            cwd=awf_project,
            check=True,
        )
        d = awf_project / ".agentic" / "done" / "TODO-0001"
        d.mkdir(parents=True, exist_ok=True)
        (d / "TODO.md").write_text("# TODO-0001\n", encoding="utf-8")

        result = api.wait_for_event(awf_project, timeout=2)

        assert result.event_type == "done"
        assert "TODO-0001" in result.message

    def test_idle_when_no_state_and_no_evidence(self, awf_project):
        """Fresh project: no state, no cycle signs → 'idle' (unchanged)."""
        result = api.wait_for_event(awf_project, timeout=1)
        assert result.event_type == "idle"
        assert "not running" in result.message

    def test_done_during_wait_when_pipeline_exits(self, awf_project):
        """Markers present at call start; the pipeline exits mid-wait →
        'done' with the cycle sign at the next poll (≤ poll_interval)."""
        import subprocess
        import threading
        import time as _time

        from awf.pipeline_state import clear_state

        write_state(
            awf_project,
            stage_name="agent-impl",
            stage_kind="execute",
            stage_idx=2,
            todo_id="TODO-0002",
        )

        def exit_later():
            _time.sleep(0.3)
            subprocess.run(
                ["git", "commit", "--allow-empty", "-qm", "awf(verify): TODO-0002"],
                cwd=awf_project,
                check=True,
            )
            d = awf_project / ".agentic" / "done" / "TODO-0002"
            d.mkdir(parents=True, exist_ok=True)
            (d / "TODO.md").write_text("# TODO-0002\n", encoding="utf-8")
            clear_state(awf_project)
            write_state(awf_project, phase="done")

        threading.Thread(target=exit_later, daemon=True).start()
        result = api.wait_for_event(awf_project, timeout=5, poll_interval=1)

        assert result.event_type == "done"
        assert "TODO-0002" in result.message


class TestSalvageKillLeftover:
    """RUN7 #1: a kill after a salvage attempt leaves ONLY the salvage
    counter in state (_clear_state_keep_salvage, awf/api/pipeline.py) — the
    stage markers are wiped and no phase=done is written. When the project
    carries an OLDER cycle's evidence (done/<id>/ or an awf commit), that
    leftover must not produce a 'done' for the old TODO — the QA TODO-0056
    scratch repro: `EVENT: done, MSG: TODO-0001 committed and archived...`
    in a run whose next TODO was the killed one. The leftover keeps the old
    wait behavior (timeout), never a 'done' for a past cycle.
    """

    @staticmethod
    def _finish_cycle(project, todo_id: str) -> None:
        """The cycle's signs: the verify commit + the done/<id>/ archive
        (what _last_completed_todo looks for)."""
        import subprocess

        subprocess.run(
            ["git", "commit", "--allow-empty", "-qm", f"awf(verify): {todo_id}"],
            cwd=project, check=True,
        )
        d = project / ".agentic" / "done" / todo_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")

    @staticmethod
    def _simulate_kill_after_salvage(project, todo_id: str = "TODO-0002") -> None:
        """Mirror kill_pipeline's tail after a salvage attempt
        (_clear_state_keep_salvage): the stage state is cleared, only
        salvage_count/salvage_count_key survive."""
        from awf.pipeline_state import clear_state

        write_state(
            project,
            stage_name="agent-impl",
            stage_kind="execute",
            stage_idx=2,
            todo_id=todo_id,
            pipeline_pid=99999,
        )
        clear_state(project)
        write_state(
            project,
            salvage_count=1,
            salvage_count_key=f"{todo_id}:execute",
        )

    def test_no_false_done_for_previous_cycle(self, awf_project):
        """Battle case (QA TODO-0056): TODO-0001 finished, TODO-0002 killed
        after salvage → no 'done' for TODO-0001, the old wait (timeout)."""
        self._finish_cycle(awf_project, "TODO-0001")
        self._simulate_kill_after_salvage(awf_project, "TODO-0002")

        result = api.wait_for_event(awf_project, timeout=2, poll_interval=1)

        assert result.event_type == "timeout"
        assert "TODO-0001" not in result.message

    def test_no_false_done_when_kill_happens_during_wait(self, awf_project):
        """The leftover appears mid-wait (the kill lands between polls):
        the poll loop keeps the old wait behavior too."""
        import threading
        import time as _time

        self._finish_cycle(awf_project, "TODO-0001")
        write_state(
            awf_project,
            stage_name="agent-impl",
            stage_kind="execute",
            stage_idx=2,
            todo_id="TODO-0002",
        )

        def kill_later():
            _time.sleep(0.3)
            from awf.pipeline_state import clear_state

            clear_state(awf_project)
            write_state(
                awf_project,
                salvage_count=1,
                salvage_count_key="TODO-0002:execute",
            )

        threading.Thread(target=kill_later, daemon=True).start()
        result = api.wait_for_event(awf_project, timeout=2, poll_interval=1)

        assert result.event_type == "timeout"
        assert "TODO-0001" not in result.message

    def test_done_after_real_clean_exit_of_killed_cycle(self, awf_project):
        """The same cycle finishes for real afterwards (the orchestrator's
        clean exit) → 'done' names the finished TODO, not the old one."""
        self._finish_cycle(awf_project, "TODO-0001")
        self._simulate_kill_after_salvage(awf_project, "TODO-0002")
        self._finish_cycle(awf_project, "TODO-0002")
        from awf.pipeline_state import clear_state

        clear_state(awf_project)
        write_state(awf_project, phase="done", goal="test goal", normalized=True)

        result = api.wait_for_event(awf_project, timeout=2)

        assert result.event_type == "done"
        assert "TODO-0002" in result.message
        assert "TODO-0001" not in result.message


class TestStateIsExited:
    """RUN7 #1: the 'exited' decision — only the two cycle-end shapes
    (state absent; clean-exit leftover with phase=done) count."""

    def test_absent_state_is_exited(self):
        from awf.api.wait_event import _state_is_exited

        assert _state_is_exited(None) is True
        assert _state_is_exited({}) is True

    def test_clean_exit_leftover_is_exited(self):
        from awf.api.wait_event import _state_is_exited

        assert _state_is_exited({"phase": "done", "goal": "g", "normalized": True}) is True

    def test_salvage_kill_leftover_is_not_exited(self):
        from awf.api.wait_event import _state_is_exited

        assert _state_is_exited(
            {"salvage_count": 1, "salvage_count_key": "TODO-0002:execute"}
        ) is False
        assert _state_is_exited({"salvage_count": 1, "salvage_count_key": None}) is False

    def test_live_state_is_not_exited(self):
        from awf.api.wait_event import _state_is_exited

        assert _state_is_exited(
            {"stage_name": "agent-impl", "stage_kind": "execute", "todo_id": "TODO-0002"}
        ) is False

    def test_active_salvage_on_live_markers_is_not_exited(self):
        """The salvage flag merges onto live markers (pipeline_engine.py) —
        that state is a live pipeline, never 'exited'."""
        from awf.api.wait_event import _state_is_exited

        assert _state_is_exited(
            {
                "stage_name": "agent-impl",
                "stage_kind": "execute",
                "todo_id": "TODO-0002",
                "salvage_needed": True,
                "salvage_count": 1,
            }
        ) is False


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


class TestWaitCap:
    """RUN6 #3: the single-wait cap is honest — env > config > default 55."""

    def _set_config_cap(self, awf_project, text):
        (awf_project / ".agentic" / "config.yaml").write_text(text, encoding="utf-8")

    def test_default_without_config_or_env(self, awf_project, monkeypatch):
        from awf.api.wait_event import TRANSPORT_CAP, wait_cap

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        assert wait_cap(awf_project) == TRANSPORT_CAP
        assert wait_cap(None) == TRANSPORT_CAP

    def test_config_cap_seconds(self, awf_project, monkeypatch):
        from awf.api.wait_event import wait_cap

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        assert wait_cap(awf_project) == 300

    def test_env_wins_over_config(self, awf_project, monkeypatch):
        from awf.api.wait_event import wait_cap

        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        monkeypatch.setenv("AWF_WAIT_CAP", "420")
        assert wait_cap(awf_project) == 420

    def test_string_values_accepted(self, awf_project, monkeypatch):
        from awf.api.wait_event import wait_cap

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._set_config_cap(awf_project, 'wait:\n  cap_seconds: "300"\n')
        assert wait_cap(awf_project) == 300
        monkeypatch.setenv("AWF_WAIT_CAP", " 270 ")
        assert wait_cap(awf_project) == 270

    def test_invalid_env_falls_through_to_config(self, awf_project, monkeypatch):
        from awf.api.wait_event import wait_cap

        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        monkeypatch.setenv("AWF_WAIT_CAP", "abc")
        assert wait_cap(awf_project) == 300
        monkeypatch.setenv("AWF_WAIT_CAP", "0")
        assert wait_cap(awf_project) == 300

    def test_invalid_config_falls_back_to_default(self, awf_project, monkeypatch):
        from awf.api.wait_event import TRANSPORT_CAP, wait_cap

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._set_config_cap(awf_project, "wait:\n  cap_seconds: -5\n")
        assert wait_cap(awf_project) == TRANSPORT_CAP
        self._set_config_cap(awf_project, "wait:\n  cap_seconds: fast\n")
        assert wait_cap(awf_project) == TRANSPORT_CAP

    def test_suggestion_kept_below_raised_config_cap(self, awf_project, monkeypatch):
        """12-min stage → raw 240s. With the cap raised to 300 the suggestion
        is 240 (kept), not cut to the 55s transport default (B3 old behavior)."""
        from datetime import datetime, timedelta

        from awf.api.wait_event import _suggest_timeout

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=12 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert _suggest_timeout(awf_project) == 240

    def test_suggestion_never_above_env_cap(self, awf_project, monkeypatch):
        from datetime import datetime, timedelta

        from awf.api.wait_event import _suggest_timeout

        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        monkeypatch.setenv("AWF_WAIT_CAP", "100")
        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=12 * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

        assert _suggest_timeout(awf_project) == 100

    def _write_stage_log(self, awf_project, minutes):
        from datetime import datetime, timedelta

        logs = awf_project / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = []
        for i in range(4):
            ts = (base + timedelta(minutes=minutes * i)).strftime("%Y-%m-%dT%H:%M:%S")
            lines.append(f"[{ts}Z] Stage {i}/4: stage{i} (role :: execute)")
        (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_timeout_message_cites_configured_cap(self, awf_project, monkeypatch):
        """Raised cap → the timeout message names it and drops the default
        transport wording (the 'raise mcp timeout' advice is stale then).
        20-min stage → raw 400s → clamped to the 300s cap → advice fires."""
        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._set_config_cap(awf_project, "wait:\n  cap_seconds: 300\n")
        self._write_stage_log(awf_project, minutes=20)

        write_state(
            awf_project, stage_name="agent-impl", stage_kind="execute", stage_idx=2,
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)

        assert result.event_type == "timeout"
        assert result.suggested_timeout == 300
        assert "300s" in result.message
        assert "Transport cap" not in result.message
        assert "smaller steps" in result.message

    def test_timeout_message_default_cap_unchanged(self, awf_project, monkeypatch):
        """No config/env → cap stays 55 and the B3 message is intact.
        20-min stage → raw 400s → clamped to the 55s cap → advice fires."""
        from awf.api.wait_event import TRANSPORT_CAP

        monkeypatch.delenv("AWF_WAIT_CAP", raising=False)
        self._write_stage_log(awf_project, minutes=20)

        write_state(
            awf_project, stage_name="agent-impl", stage_kind="execute", stage_idx=2,
        )
        result = api.wait_for_event(awf_project, timeout=1, poll_interval=1)

        assert result.event_type == "timeout"
        assert result.suggested_timeout == TRANSPORT_CAP
        assert "Transport cap" in result.message
        assert "smaller steps" in result.message
