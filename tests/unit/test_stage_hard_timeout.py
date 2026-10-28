"""OPS IMP3 (R3): the hard cap closes the stage with a BLOCKED note.

T6 — the hard cap is not classified by the tail: one spawn, a standard
BLOCKED note naming the stage and the cap, ``last_signal=BLOCKED-<id>``,
rc 1, no backoff.
T7 — ``awf blocked-draft`` reads that note.
T8 — a late DONE keeps the stage, the watchdog and the preflight keep
their own branches (INV9, INV3).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import pipeline_engine
from awf._errors import AwfApiError
from awf.pipeline import Stage
from awf.signal_watch import HardTimeoutError, run_subprocess_until_signal


def _stages() -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute", on_blocked="escalate"),
        Stage(name="verify", role="supervisor", kind="verify", on_approved="commit_and_next"),
    ]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "context", "logs"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# unit\n", encoding="utf-8"
    )
    (proj / ".agentic" / "inbox" / "TODO-0001.ready").write_text(
        "", encoding="utf-8"
    )
    return proj


def _mock_pipeline(monkeypatch, log_text: str, behavior):
    """Scripted worker whose ``log_holder`` points at ``log_text``."""
    journal = {"runs": 0}

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        journal["runs"] += 1
        holder = kw.get("log_holder")
        if holder is not None and log_file is not None:
            holder["log_path"] = str(log_file)
            holder["log_start_offset"] = "0"
        behavior(journal["runs"], kw)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "abc123")
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)

    from awf import commit_plan, verify

    monkeypatch.setattr(
        pipeline_engine, "_maybe_commit",
        lambda *a, **kw: commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, "policy 'next' does not commit"
        ),
    )
    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)
    monkeypatch.setattr(verify, "work_fingerprint", lambda *a, **kw: "")

    import time as _time_mod

    ticks = iter(range(10_000))
    monkeypatch.setattr(_time_mod, "monotonic", lambda: float(next(ticks)))
    monkeypatch.setattr("time.sleep", lambda _dt: None)

    log_file = None

    def prepare(tmp_path: Path) -> Path:
        nonlocal log_file
        log_file = tmp_path / "worker.out"
        log_file.write_text(log_text, encoding="utf-8")
        return log_file

    return journal, prepare


def _run_stage(project: Path, tmp_path: Path):
    stages = _stages()
    return pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo="TODO-0001", project_dir=project,
        config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        pipeline_name=None,
    )


def _note(project: Path) -> Path:
    return project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md"


def test_hard_cap_writes_a_blocked_note_without_a_second_spawn(
    project, tmp_path, monkeypatch,
):
    """AC8: cap 1s, tail ``429 overloaded`` → one spawn, BLOCKED, rc 1."""
    journal, prepare = _mock_pipeline(
        monkeypatch,
        "429 overloaded\n",
        lambda run_no, kw: (_ for _ in ()).throw(HardTimeoutError(1)),
    )
    prepare(tmp_path)
    log_lines: list[str] = []
    monkeypatch.setattr(
        pipeline_engine, "_log",
        lambda *a, **kw: log_lines.append(str(a[1]) if len(a) > 1 else ""),
    )

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1, "the hard cap must not spawn a second worker"
    assert result[2] == 1
    assert any(
        "hard timeout on worker — BLOCKED, stage stopped" in line
        for line in log_lines
    ), "the stage log names the hard cap and the BLOCKED stop"
    assert _note(project).is_file()
    assert (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").is_file()

    text = _note(project).read_text(encoding="utf-8")
    for section in ("## ВЕРДИКТ", "## НАХОДКИ", "## НАПРАВЛЕНИЯ-ФИКСА", "## ПЕРЕНОСЫ"):
        assert section in text
    assert "worker" in text and "1s" in text, "the verdict names stage and cap"
    assert "429 overloaded" in text, "the findings quote the log tail"

    from awf.pipeline_state import read_state

    state = read_state(project)
    assert state.get("last_signal") == "BLOCKED-TODO-0001"
    assert state.get("net_retry_count") in (None, 0), "the cap must not spend the net budget"
    assert state.get("sw_retry_count") in (None, 0), "the cap must not spend the silent budget"


def test_blocked_draft_reads_the_hard_timeout_note(project, tmp_path):
    """AC9: the note keeps the slots ``awf blocked-draft`` fills."""
    from awf import pipeline_engine as pe
    from awf.api.hygiene import blocked_draft

    pe._stop_on_hard_timeout(
        project, "TODO-0001", "worker", tmp_path, 30, "hung\n",
    )

    draft = blocked_draft(project, "TODO-0001")

    assert "## REVIEW-фикс" in draft.review_text
    assert "кап: 30s" in draft.review_text
    assert "hung" in draft.review_text
    assert "подними timeout" in draft.review_text, "the fix directions survive"
    assert "Скоуп: …" in draft.review_text
    assert "Переносы: …" in draft.review_text

    (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").unlink()
    with pytest.raises(AwfApiError):
        blocked_draft(project, "TODO-0001")


def test_late_done_in_the_kill_window_completes_the_stage(project, monkeypatch):
    """AC10: a signal that landed while the cap fired is not a death."""
    outbox = project / ".agentic" / "outbox"
    signal_file = outbox / "DONE-TODO-0001.ready"
    holder: dict[str, str] = {}

    class _HungPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999  # never a real group — killpg must not run
            self.returncode = None
            signal_file.write_text("", encoding="utf-8")
            (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -15

        def terminate(self):
            self.kill()

    clock = {"t": 1000.0}

    def fake_sleep(dt: float) -> None:
        clock["t"] += dt

    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _HungPopen)
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("time.sleep", fake_sleep)

    result = run_subprocess_until_signal(
        ["hung-worker"], cwd=project, watch_paths=[signal_file],
        logs_dir=project / ".agentic" / "logs", log_name="hung.out",
        signal_holder=holder, no_output_timeout=0, hard_timeout=30,
    )

    assert result.returncode == 0, "the late signal finishes the stage"
    assert holder.get("signal") == "DONE-TODO-0001.ready"
    assert not _note(project).exists(), "a completed unit is not BLOCKED"


def test_watchdog_keeps_its_own_branch_without_a_blocked_note(
    project, tmp_path, monkeypatch,
):
    """AC10/INV9: the silence watchdog is not the hard cap."""
    journal, prepare = _mock_pipeline(
        monkeypatch,
        "",
        lambda run_no, kw: (_ for _ in ()).throw(
            TimeoutError("Worker produced no output for 5 min (watchdog) — "
                         "process tree killed")
        ),
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert result[2] == 1
    assert not _note(project).exists(), "only the hard cap writes BLOCKED"


def test_preflight_timeout_keeps_the_network_backoff(project, tmp_path, monkeypatch):
    """AC10/INV3: the preflight timeout stays a network class with a retry."""
    def behavior(run_no, kw):
        if run_no >= 2:
            outbox = project / ".agentic" / "outbox"
            (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
            (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    journal, prepare = _mock_pipeline(
        monkeypatch,
        "",
        lambda run_no, kw: (
            behavior(run_no, kw) if run_no >= 2 else (_ for _ in ()).throw(
                TimeoutError(
                    "Model endpoint http://localhost:1 unreachable for 5s — "
                    "worker not started"
                )
            )
        ),
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert result[2] == 0
    assert journal["runs"] == 2, "the preflight timeout gets the net retry"
    assert not _note(project).exists(), "preflight is network, not BLOCKED"


def test_watcher_raises_the_typed_error_on_the_hard_cap(tmp_path, monkeypatch):
    """AC8 seam: the watcher itself raises ``HardTimeoutError``.

    The engine's branch keys off the type, so a plain ``TimeoutError`` here
    would silently hand the cap back to the tail classifier. The engine
    test mocks the worker and would stay green after such a revert.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    signal_file = outbox / "DONE-TODO-0001.ready"

    class _HungPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999  # never a real group — killpg must not run
            self.returncode = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -15

        def terminate(self):
            self.kill()

    clock = {"t": 1000.0}

    def fake_sleep(dt: float) -> None:
        clock["t"] += dt

    monkeypatch.setattr("awf.signal_watch.subprocess.Popen", _HungPopen)
    monkeypatch.setattr("time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("time.sleep", fake_sleep)

    with pytest.raises(HardTimeoutError) as caught:
        run_subprocess_until_signal(
            ["hung-worker"], cwd=tmp_path, watch_paths=[signal_file],
            logs_dir=tmp_path, log_name="hung.out",
            no_output_timeout=0, hard_timeout=30,
        )

    assert caught.value.seconds == 30
    assert "did not produce signal within 30s" in str(caught.value)


def test_a_signal_on_disk_at_the_cap_is_not_rewritten_as_blocked(
    project, tmp_path, monkeypatch,
):
    """AC10/INV1: BLOCKED requires no signal on disk.

    The watcher already returns a signal found during the kill; the engine's
    own guard covers the last window. Without it a completed unit would get
    a BLOCKED note over its DONE.
    """
    outbox = project / ".agentic" / "outbox"

    def done_then_cap(run_no, kw):
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
        raise HardTimeoutError(1)

    journal, prepare = _mock_pipeline(monkeypatch, "429 overloaded\n", done_then_cap)
    prepare(tmp_path)

    _run_stage(project, tmp_path)

    assert journal["runs"] == 1
    assert not _note(project).exists(), "a completed unit is not BLOCKED"
    assert not (outbox / "BLOCKED-TODO-0001.ready").exists()

    from awf.pipeline_state import read_state

    state = read_state(project) or {}
    assert state.get("last_signal") != "BLOCKED-TODO-0001"
