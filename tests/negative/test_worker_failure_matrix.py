"""NEG-2026-09 layer 1: worker failure matrix.

Each scenario scripts what the worker does during a stage run and asserts
awf's reaction (retry budget, salvage note, state) plus invariants:
- no orphan signal files left in outbox,
- state never claims salvage when a valid signal was honored,
- the retry budget resets per stage entry.

These are *negative* paths: every scenario ends in failure, recovery, or
stop — never in the happy path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import pipeline_engine
from awf.pipeline import Stage


def _stages() -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute", on_blocked="escalate"),
        Stage(name="verify", role="supervisor", kind="verify", on_approved="commit_and_next"),
    ]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    (proj / ".agentic" / "outbox").mkdir(parents=True)
    (proj / ".agentic" / "context").mkdir(parents=True)
    (proj / ".agentic" / "logs").mkdir(parents=True)
    return proj


def _mock_pipeline(monkeypatch, behavior, baseline_sha: str | None = "abc123"):
    """Wire the stage loop to a scripted worker; return the run journal."""
    journal = {"runs": 0, "retry_notes": [], "monotonic": iter(range(10_000))}

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        journal["runs"] += 1
        journal["retry_notes"].append(kw.get("retry_note"))
        behavior(journal["runs"], kw)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: baseline_sha)
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "_maybe_commit", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    import time as _time_mod

    monkeypatch.setattr(_time_mod, "monotonic", lambda: float(next(journal["monotonic"])))

    from awf import verify

    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

    return journal


def _run_stage(project: Path, tmp_path: Path, stages: list[Stage] | None = None):
    stages = stages or _stages()
    return pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo="TODO-0001", project_dir=project,
        config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        pipeline_name=None,
    )


def _state(project: Path) -> dict:
    from awf.pipeline_state import read_state
    return read_state(project) or {}


class TestSilentExitFamily:
    def test_two_silent_runs_then_success(self, project, tmp_path, monkeypatch):
        """Retry budget: silent → silent → DONE. Pipeline proceeds, no salvage."""
        def behavior(run_no, kw):
            if run_no == 3:
                outbox = project / ".agentic" / "outbox"
                (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
                (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")

        journal = _mock_pipeline(monkeypatch, behavior)
        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 3
        assert journal["retry_notes"][0] is None
        assert "RETRY 1" in journal["retry_notes"][1]
        assert "RETRY 2" in journal["retry_notes"][2]
        assert result[2] == 0
        assert not _state(project).get("salvage_needed")
        assert not (project / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md").exists()

    def test_silent_with_real_diff_no_retry(self, tmp_path, monkeypatch):
        """Worker wrote a tracked change → no rerun risk, straight to salvage."""
        import subprocess

        # Real git repo: baseline sha = HEAD, worker edits a tracked file.
        project = tmp_path / "repo"
        project.mkdir()
        for cmd in (
            ["git", "init", "-q"],
            ["git", "config", "user.email", "t@t.t"],
            ["git", "config", "user.name", "tester"],
        ):
            subprocess.run(cmd, cwd=project, check=True)
        (project / "README.md").write_text("init\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
        (project / ".agentic" / "outbox").mkdir(parents=True)
        (project / ".agentic" / "context").mkdir(parents=True)
        (project / ".agentic" / "logs").mkdir(parents=True)

        from awf.git_utils import current_sha
        sha = current_sha(project)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")

        journal = _mock_pipeline(monkeypatch, behavior, baseline_sha=sha)
        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 1  # work evidence stops the retry loop
        assert result[2] == 1
        assert _state(project).get("salvage_count") == 1

    def test_stale_signal_from_previous_run_not_honored(self, project, tmp_path, monkeypatch):
        """A leftover DONE from an earlier attempt must not satisfy a new run."""
        from awf.signals import clean_stage_signals, expected_signal_prefixes

        outbox = project / ".agentic" / "outbox"
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")  # stale

        def behavior(run_no, kw):
            # run_agent_stage cleans this kind's signals before spawning.
            clean_stage_signals(outbox, "TODO-0001", *expected_signal_prefixes("execute"))

        journal = _mock_pipeline(monkeypatch, behavior)
        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 3  # stale signal never counted as success
        assert result[2] == 1
        assert not (outbox / "DONE-TODO-0001.ready").exists()


class TestCrashFamily:
    @pytest.mark.parametrize("exc", [RuntimeError("exit code 1"), TimeoutError("hung")])
    def test_crash_stops_without_retry_or_salvage(self, project, tmp_path, monkeypatch, exc):
        """Non-zero exit / hang → hard stop, no auto-retry, no salvage note.

        Documents current behavior: a crash is treated as infrastructure
        failure, not a worker mistake. The supervisor learns from status,
        not from a SALVAGE note.
        """
        def behavior(run_no, kw):
            raise exc

        journal = _mock_pipeline(monkeypatch, behavior)
        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 1
        assert result[2] == 1
        assert not (project / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md").exists()


class TestSignalRaceFamily:
    def test_signal_in_wait_window_honored(self, project, tmp_path, monkeypatch):
        """DONE during the 30s wait window → success, no retry."""
        journal = _mock_pipeline(monkeypatch, lambda run_no, kw: None)
        monkeypatch.setattr(
            pipeline_engine, "wait_for_signal",
            lambda *a, **kw: "DONE-TODO-0001.ready",
        )

        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 1
        assert result[2] == 0

    def test_signal_landing_before_cleanup_is_not_wiped(self, project, tmp_path, monkeypatch):
        """TOCTOU guard: DONE written between the wait timeout and the retry
        cleanup must be honored — not deleted by clean_stage_signals.

        Repro: detect_work_evidence (called right before cleanup) writes the
        signal as a side effect. Without a re-read before cleaning, the valid
        DONE gets wiped, the stage reruns needlessly, and a later crash would
        lose the signal entirely.
        """
        outbox = project / ".agentic" / "outbox"

        journal = _mock_pipeline(monkeypatch, lambda run_no, kw: None)

        from awf import verify

        def race_window(*a, **kw):
            (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
            return False  # "no work evidence" — awf heads into the retry branch

        monkeypatch.setattr(verify, "detect_work_evidence", race_window)

        result = _run_stage(project, tmp_path)

        assert journal["runs"] == 1, "signal must be honored, not retried"
        assert result[2] == 0
        assert (outbox / "DONE-TODO-0001.ready").exists()
        assert not _state(project).get("salvage_needed")


class TestRollbackFallbackFamily:
    def test_missing_rollback_target_escalates_instead_of_stopping(
        self, project, tmp_path, monkeypatch, capsys,
    ):
        """NEG-2: rejection with a phantom rollback target → supervisor replan.

        Before the fix the dispatcher called _handle_rollback → target not
        found → exit 1 (hard stop mid-run). Now it falls back to the
        escalation path: the supervisor gets the rejection and replans.
        """
        inbox = project / ".agentic" / "inbox"
        inbox.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# Task\n", encoding="utf-8")
        (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")

        def behavior(run_no, kw):
            (project / ".agentic" / "outbox" / "REVIEW-REJECTED-TODO-0001.ready").write_text(
                "", encoding="utf-8",
            )

        journal = _mock_pipeline(monkeypatch, behavior)
        stages = _stages()
        stages[1].on_rejected = "rollback_to:ghost"

        result = _run_stage(project, tmp_path, stages=stages)

        assert journal["runs"] == 1
        assert result[2] == 0, "pipeline must survive a bad rollback target"
        err = capsys.readouterr().err
        assert "rollback target 'ghost' not found" in err
