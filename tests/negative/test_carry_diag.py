"""CARRY IMP1 (R1, R12): death diagnostics and provider-failure classes.

T1 — the worker-log tail is read only when there is no signal; both
"signal on disk" places speak the same text; the tail is capped at 64 KiB
from the current attempt's start offset.
T2 — auth is its own class (BLOCKED note, no second spawn); 5xx/429 are a
network-class retry; a bare number in ordinary text is not a failure.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import _net, pipeline_engine
from awf._worker_exit import (
    classify_worker_exit,
    signal_present_line,
    worker_exit_line,
    worker_log_tail,
)
from awf.pipeline import Stage
from awf.signal_watch import _late_signal_done, run_subprocess_until_signal

# ── T1: lazy tail, one signal text, 64 KiB ───────────────────────────────


def test_signal_suppresses_the_tail_reader():
    """AC1: with a signal the tail argument is never evaluated."""
    called = {"n": 0}

    def boom() -> str:
        called["n"] += 1
        raise AssertionError("the tail must not be read when a signal is present")

    assert worker_exit_line(1, 12.4, tail_fn=boom, signal_present=True) == ""
    assert called["n"] == 0


def test_one_signal_text_with_the_name():
    """AC1/AC3: one wording, the signal name is present."""
    assert signal_present_line("DONE") == "U6b: signal present (DONE)"
    assert signal_present_line(None) == "U6b: signal present (unknown)"


def test_late_signal_uses_the_same_text(tmp_path, monkeypatch):
    lines: list[str] = []
    monkeypatch.setattr("awf.signal_watch._log", lambda _d, msg: lines.append(msg))
    holder: dict[str, str] = {}

    done = _late_signal_done(["cmd"], "DONE", holder, tmp_path)

    assert done is not None and done.returncode == 0
    assert holder["signal"] == "DONE"
    assert lines == ["U6b: signal present (DONE)"]
    assert not any("already detected" in line for line in lines)


def test_no_unnamed_signal_text_left_in_source():
    """AC3: the second, nameless wording is gone from EVERY module that
    could print "a signal is on disk" — scanning only ``signal_watch`` would
    let ``agent_stage`` (or the shared builder) reintroduce it unnoticed."""
    awf = Path(__file__).resolve().parents[2] / "awf"
    for name in ("signal_watch.py", "agent_stage.py", "_worker_exit.py"):
        source = (awf / name).read_text(encoding="utf-8")
        assert "signal already detected" not in source, name


def test_tail_is_capped_and_starts_at_the_attempt_offset(tmp_path):
    """AC2: last 64 KiB from ``log_start_offset``, not the previous run."""
    log = tmp_path / "worker.out"
    size = 70 * 1024
    body = b"x" * size
    body = b"START-MARK" + body[10:]
    body = body[:-11] + b"END-MARKER!"
    log.write_bytes(body)

    tail = _net.read_log_tail(log, start_offset=100)

    assert len(tail.encode("utf-8")) == 65536
    assert "START-MARK" not in tail
    assert "END-MARKER!" in tail
    assert tail.encode("utf-8")[0:1] == body[size - 65536:size - 65535]


def test_attempt_offset_cuts_a_marker_inside_the_tail_window(tmp_path):
    """AC2: the holder offset is honoured even when the previous run's marker
    is INSIDE the last 64 KiB. The capped-window test above cannot see this:
    its marker sits at byte 0, which the 64 KiB window cuts anyway, so it
    would pass with the offset ignored."""
    size = 70 * 1024
    log = tmp_path / "worker.out"
    body = bytearray(b"x" * size)
    marker = b"OLD-RUN-MARK"
    pos = size - 65536 + 10
    body[pos:pos + len(marker)] = marker
    log.write_bytes(bytes(body))
    offset = pos + 1

    tail = worker_log_tail({"log_path": str(log), "log_start_offset": str(offset)})

    assert "OLD-RUN-MARK" not in tail
    assert len(tail.encode("utf-8")) == size - offset
    # sanity: the same marker IS in the 64 KiB window when the offset is ignored
    assert "OLD-RUN-MARK" in _net.read_log_tail(log)


def test_empty_path_and_missing_file_give_empty(tmp_path):
    assert worker_log_tail({"log_path": ""}) == ""
    assert worker_log_tail({"log_path": str(tmp_path / "nope.out")}) == ""


def test_exit_line_is_one_text_with_class():
    """AC3: rc, exit signal, duration and class in one line."""
    assert worker_exit_line(1, 12.4) == (
        "worker-exit: rc=1 signal=none duration=12s class=silent"
    )
    assert "signal=SIGKILL" in worker_exit_line(137, 12.6)
    assert "signal=SIGKILL" in worker_exit_line(-9, 12.6)
    assert "class=network" in worker_exit_line(1, 12.4, "503 Service Unavailable")


def test_the_lazy_tail_is_still_read_without_a_signal():
    """AC1/AC2: laziness must not turn into "never read".

    The signal check skips the reader; with NO signal the tail_fn is the
    ONLY source of the class, so a wiring that dropped it would silently
    label every death ``silent``."""
    assert "class=network" in worker_exit_line(
        1, 3.0, tail_fn=lambda: "503 Service Unavailable"
    )
    assert "class=auth" in worker_exit_line(
        1, 3.0, tail_fn=lambda: "401 Invalid credential"
    )


def test_agent_stage_wiring_is_lazy_with_a_signal(tmp_path, monkeypatch):
    """AC1 integration: the agent_stage call site must not read the tail
    eagerly. The defect (C2) was the argument being evaluated before the
    signal check, so pinning the pure function alone would not catch a
    regression in the wiring."""
    from awf import agent_stage

    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer\n", encoding="utf-8"
    )
    outbox = proj / ".agentic" / "outbox"

    def fake_run(*_a, **_kw):
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

        class _R:
            returncode = 1

        return _R()

    def boom(*_a, **_kw):
        raise AssertionError("the tail must not be read when a signal is present")

    monkeypatch.setattr("awf.signal_watch.run_subprocess_until_signal", fake_run)
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "worker_log_tail", boom)
    lines: list[str] = []
    monkeypatch.setattr(agent_stage, "_log", lambda _d, msg: lines.append(str(msg)))

    agent_stage.run_agent_stage(
        Stage(name="agent-dev", role="developer", kind="execute"),
        "TODO-0001", proj, {}, proj / ".agentic" / "logs",
    )

    assert any("U6b: signal present (DONE-TODO-0001)" in m for m in lines), lines


def test_retry_attempt_does_not_inherit_the_previous_log_tail(tmp_path, monkeypatch):
    """R1/AC2: a death is classified by THIS attempt's log.

    The retried stage reuses ``log_holder``. An attempt that dies before it
    opens its own log (a preflight timeout raises inside the watcher) used to
    keep the earlier attempt's ``log_path``, so the engine read the old tail:
    an oom-like attempt 1 quoting ``HTTP 401`` became a false auth BLOCK on
    attempt 2 (the exit code is reset, the log pointer was not).
    """
    from awf import agent_stage

    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "developer.md").write_text(
        "# Developer\n", encoding="utf-8"
    )
    logs = proj / ".agentic" / "logs"
    log = logs / "awf-developer-TODO-0001.out"
    log.write_text("HTTP 401 unauthorized\n", encoding="utf-8")

    holder: dict[str, str] = {}
    calls = {"n": 0}

    def fake_run(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            holder["log_path"] = str(log)
            holder["log_start_offset"] = "0"

            class _R:
                returncode = 0

            return _R()
        raise TimeoutError("Model endpoint unreachable — worker not started")

    monkeypatch.setattr("awf.signal_watch.run_subprocess_until_signal", fake_run)
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "_log", lambda *a, **kw: None)

    stage = Stage(name="agent-dev", role="developer", kind="execute")
    agent_stage.run_agent_stage(stage, "TODO-0001", proj, {}, logs, log_holder=holder)
    assert holder.get("log_path") == str(log), "attempt 1's own log must be recorded"
    assert holder.get("exit_code") == "0", "attempt 1's own exit code must be recorded"

    with pytest.raises(TimeoutError):
        agent_stage.run_agent_stage(stage, "TODO-0001", proj, {}, logs, log_holder=holder)

    assert "log_path" not in holder, "the dead attempt kept the previous tail"
    assert "exit_code" not in holder, "the dead attempt kept the previous exit code"
    assert agent_stage.worker_log_tail(holder) == "", (
        "the next death must not be classified by the previous attempt's log"
    )


class _FakeClock:
    """Monotonic clock advanced by the fake sleep (no real waiting)."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def monotonic(self) -> float:
        return self.t


def _install_fake_time(monkeypatch, clock: _FakeClock) -> None:
    def fake_sleep(dt: float) -> None:
        clock.t += dt

    monkeypatch.setattr("time.monotonic", clock.monotonic)
    monkeypatch.setattr("time.sleep", fake_sleep)


def _hung_worker_that_writes_the_signal(signal_file: Path):
    """Popen fake: never exits; the signal lands right AFTER the snapshot.

    That is the hard-timeout branch with ``signal_seen_at`` set — the
    worker finished logically but keeps the process alive (a flush), so the
    deadline, not the natural exit, ends the wait.
    """

    class _HungPopen:
        def __init__(self, cmd, cwd=None, **kwargs):
            self.cmd = cmd
            self.pid = 999999999  # getpgid() must fail — never killpg
            self.returncode = None
            signal_file.write_text("", encoding="utf-8")

        def poll(self):
            return None

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -15

        def terminate(self):
            self.kill()

    return _HungPopen


def test_hard_timeout_names_a_signal_seen_while_waiting(tmp_path, monkeypatch):
    """K1: the hard-timeout path names the signal from the holder.

    The unit replaced the nameless "BD-20: signal already detected" here
    with the shared text. Without this test the branch could regress to
    ``unknown`` (the holder is populated exactly for this path) and nobody
    would see it: the other two kill paths take ``_late_signal()``.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    signal_file = outbox / "DONE-TODO-0001.ready"
    holder: dict[str, str] = {}

    monkeypatch.setattr(
        "awf.signal_watch.subprocess.Popen",
        _hung_worker_that_writes_the_signal(signal_file),
    )
    _install_fake_time(monkeypatch, _FakeClock())

    result = run_subprocess_until_signal(
        ["hung-worker"], cwd=tmp_path, watch_paths=[signal_file],
        logs_dir=tmp_path, log_name="hung.out", signal_holder=holder,
        no_output_timeout=0, hard_timeout=30,
    )

    assert result.returncode == 0
    assert holder.get("signal") == signal_file.name
    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "U6b: signal present (DONE-TODO-0001)" in text
    assert "class=timeout" not in text


def test_hard_timeout_without_a_name_falls_back_to_unknown(tmp_path, monkeypatch):
    """K1: "the holder, else a re-read, else ``unknown``".

    The name can be gone from disk by the time the kill lands (a cleanup
    deleted the marker). The text must still be the shared one — not the
    nameless wording the unit removed.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    signal_file = outbox / "DONE-TODO-0001.ready"

    monkeypatch.setattr(
        "awf.signal_watch.subprocess.Popen",
        _hung_worker_that_writes_the_signal(signal_file),
    )
    _install_fake_time(monkeypatch, _FakeClock())

    from awf import signal_watch

    first = {"done": False}

    def detect_once(*_a, **_kw):
        if not first["done"]:
            first["done"] = True
            return signal_file.name
        return None

    # The loop's detect sets ``signal_seen_at``; every later read (the kill
    # path) finds nothing — the only name left would be "unknown".
    monkeypatch.setattr(signal_watch, "_detect_new_signal", detect_once)

    result = run_subprocess_until_signal(
        ["hung-worker"], cwd=tmp_path, watch_paths=[signal_file],
        logs_dir=tmp_path, log_name="hung.out", signal_holder=None,
        no_output_timeout=0, hard_timeout=30,
    )

    assert result.returncode == 0
    text = (tmp_path / "orchestrator.log").read_text(encoding="utf-8")
    assert "U6b: signal present (unknown)" in text
    assert "class=timeout" not in text


# ── T2: failure classes and the BLOCKED note ─────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("401 Invalid credential", "auth"),
        ("HTTP 401 unauthorized", "auth"),
        ("403 Forbidden: invalid_api_key", "auth"),
        ("HTTP 500", "transient"),
        ("503 Service Unavailable", "transient"),
        ("500 Internal Server Error", "transient"),
        ("429 Too Many Requests", "transient"),
        ("HTTP/1.1 502 Bad Gateway", "transient"),
        ("overloaded", "transient"),
        ("connect ECONNREFUSED", "network"),
        ("Connection reset by peer", "network"),
        ("ETIMEDOUT", "network"),
        ("request timeout", "network"),
        ("1500 passed", ""),
        ("500 passed", ""),
        ("took 429ms", ""),
        ("step 500 of 1000", ""),
        ("503", ""),
        ("status: 200", ""),
        ("", ""),
        # D6: a 5xx next to a 401 is an endpoint problem, not a dead key
        ("401 Invalid credential\nHTTP 503", "network"),
        # AUD1: the code AND the word are required — a project's own failing
        # test named ``unauthorized`` must not block the stage.
        ("FAILED test_auth.py::test_unauthorized_access_is_rejected", ""),
        ("unauthorized", ""),
        ("invalid credential", ""),
        # the code must be its own token and on the SAME line as the word —
        # an identifier fragment or a cross-line co-occurrence is a project's
        # own log, not a dead key
        ("FAILED test_401_credential_expiry", ""),
        ("step 401 of 1000\ncredential rotated", ""),
        # AUD3: real provider strings
        ("statusCode: 503", "transient"),
        ("statusCode: 429", "transient"),
        ("statusCode: 500", "transient"),
        # AUD3 names the GENERIC form as a real provider string: a 5xx token
        # is a server error, not just the concrete 500/502/503/504 codes.
        ("statusCode: 5xx", "transient"),
        ("HTTP 5xx", "transient"),
        ("code: 5xx", "transient"),
        # a JSON dump of the same provider error (quoted key)
        ('{"statusCode":503}', "transient"),
        # R12: the bare reason phrases are transient too
        ("Service Unavailable", "transient"),
        ("Too Many Requests", "transient"),
        # R12/K12: the network list is CLOSED and ``upstream request failed``
        # alone is not on it — the provider also uses it for request-shape
        # errors (``[unsupported_request]`` stays silent).
        ("Upstream request failed", ""),
        ("Upstream request failed: [unsupported_request]", ""),
        ("[unsupported_request]", ""),
        ("Bad Request", ""),
        ("All workers are unavailable", "network"),
        ("Endpoint is unavailable", "network"),
        ("timed out waiting for an admission slot", "network"),
        ("the cell is at capacity", "network"),
        # the real opencode provider shape: INSIDE the provider wrapper a
        # code OR a credential phrase is enough (K12).
        ("AI_APICallError: statusCode: 401", "auth"),
        ("AI_APICallError: statusCode: 401 Invalid credential", "auth"),
        ("AI_APICallError: statusCode: 403 invalid api key", "auth"),
        ("AI_APICallError: Upstream request failed: Invalid credential", "auth"),
        ("AI_APICallError: unauthorized", "auth"),
        ("error.error=invalid api key", "auth"),
        # a quoted JSON code plus the phrase is code+phrase outside the
        # wrapper (AC32)
        ('{"code": 401, "message": "invalid api key"}', "auth"),
    ],
)
def test_classify_failure_matrix(text, expected):
    assert _net.classify_failure(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "connect failed",
        "reset by peer",
        "request timeout",
        "timed out waiting for an admission slot",
    ],
)
def test_generic_connect_reset_timeout_are_network_without_a_vendor_marker(text):
    """AC32: the words are network on their own, not only inside the five
    vendor markers (ECONNREFUSED / Connection reset / ETIMEDOUT). A typo in
    the generic regex would otherwise stay invisible: every positive case in
    the matrix also contains an old marker."""
    assert _net.classify_failure(text) == "network"
    assert _net.is_network_failure(text)


def test_transient_shares_the_network_retry_and_the_line_class():
    for text in ("HTTP 500", "503 Service Unavailable", "429 Too Many Requests", "overloaded"):
        assert _net.is_network_failure(text)
        assert classify_worker_exit(1, text) == "network"
    assert classify_worker_exit(1, "401 Invalid credential") == "auth"


# ── integration: both death branches ─────────────────────────────────────


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
    """Scripted worker whose log_holder points at ``log_text``."""
    journal = {"runs": 0, "retry_notes": [], "monotonic": iter(range(10_000))}
    log_file = None

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        journal["runs"] += 1
        journal["retry_notes"].append(kw.get("retry_note"))
        holder = kw.get("log_holder")
        if holder is not None and log_file is not None:
            holder["log_path"] = str(log_file)
            holder["log_start_offset"] = 0
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

    monkeypatch.setattr(_time_mod, "monotonic", lambda: float(next(journal["monotonic"])))
    monkeypatch.setattr("time.sleep", lambda _dt: None)

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


def test_auth_death_writes_the_standard_blocked_note(project, tmp_path, monkeypatch):
    """AC30: auth → BLOCKED note + last_signal, exactly one spawn."""
    journal, prepare = _mock_pipeline(
        monkeypatch, "401 Invalid credential\n", lambda run_no, kw: None,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1, "auth must not spawn a second worker"
    assert result[2] == 1

    note = project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md"
    text = note.read_text(encoding="utf-8")
    for section in ("## ВЕРДИКТ", "## НАХОДКИ", "## НАПРАВЛЕНИЯ-ФИКСА", "## ПЕРЕНОСЫ"):
        assert section in text
    # AUD4: the note quotes the log line that named the failure.
    assert "401 Invalid credential" in text

    # AUD2: the note and the marker are a pair — the closure every counter
    # and the wait-for-event reader key off.
    assert (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").is_file()

    from awf.api.hygiene import blocked_draft, unblock_todo
    from awf.api.lifecycle import get_status
    from awf.pipeline_state import read_state

    state = read_state(project)
    assert state.get("last_signal") == "BLOCKED-TODO-0001"
    assert state.get("net_retry_count") in (None, 0), "auth must not spend the net budget"
    assert state.get("sw_retry_count") in (None, 0), "auth must not spend the silent budget"
    draft = blocked_draft(project, "TODO-0001")
    assert "## REVIEW-фикс" in draft.review_text

    status = get_status(project)
    assert status.blocked_count == 1
    assert "TODO-0001" in status.blocked_ids

    unblock_todo(project, "TODO-0001")
    assert get_status(project).blocked_count == 0
    assert not (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").exists()


def test_auth_crash_branch_blocks_without_a_respawn(project, tmp_path, monkeypatch):
    """AC30, crash branch: a crash whose log names a dead credential is the
    same BLOCKED stop as a silent auth exit. T2 says BOTH death branches."""
    def behavior(run_no, kw):
        raise RuntimeError("agent stage crashed")

    journal, prepare = _mock_pipeline(
        monkeypatch, "401 Invalid credential\n", behavior,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1, "auth must not spawn a second worker"
    assert result[2] == 1
    text = (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    for section in ("## ВЕРДИКТ", "## НАХОДКИ", "## НАПРАВЛЕНИЯ-ФИКСА", "## ПЕРЕНОСЫ"):
        assert section in text

    from awf.pipeline_state import read_state

    assert read_state(project).get("last_signal") == "BLOCKED-TODO-0001"


@pytest.mark.parametrize(
    "line",
    [
        "AI_APICallError: statusCode: 401",
        "AI_APICallError: Upstream request failed: Invalid credential",
    ],
)
def test_provider_wrapper_auth_blocks_without_a_respawn(
    project, tmp_path, monkeypatch, line,
):
    """AC30: the two real provider strings reach the SAME BLOCKED stop."""
    journal, prepare = _mock_pipeline(
        monkeypatch, line + "\n", lambda run_no, kw: None,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1
    assert result[2] == 1
    note = project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md"
    assert "## ВЕРДИКТ" in note.read_text(encoding="utf-8")

    from awf.pipeline_state import read_state

    assert read_state(project).get("last_signal") == "BLOCKED-TODO-0001"


@pytest.mark.parametrize(
    "line",
    [
        "AI_APICallError: statusCode: 401",
        "AI_APICallError: Upstream request failed: Invalid credential",
    ],
)
def test_provider_wrapper_auth_blocks_on_the_crash_branch_too(
    project, tmp_path, monkeypatch, line,
):
    """T2: BOTH death branches read R12. The real provider strings must
    reach the same BLOCKED stop when the worker CRASHES (RuntimeError), not
    only on the silent-exit branch — the two branches are separate code."""
    def behavior(run_no, kw):
        raise RuntimeError("agent stage crashed")

    journal, prepare = _mock_pipeline(monkeypatch, line + "\n", behavior)
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1, "auth must not spawn a second worker"
    assert result[2] == 1
    note = project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md"
    assert "## ВЕРДИКТ" in note.read_text(encoding="utf-8")

    from awf.pipeline_state import read_state

    assert read_state(project).get("last_signal") == "BLOCKED-TODO-0001"


def test_crash_after_a_signal_is_not_classified(project, tmp_path, monkeypatch):
    """R1 on the ENGINE surface: a crash AFTER the worker's signal must not
    read the log tail at all.

    The crash branch used to classify unconditionally. A signal followed by
    a failure in post-processing (the handoff write) would then read the
    worker's unrelated log — an auth-looking line there would BLOCK a unit
    that is already done.
    """
    outbox = project / ".agentic" / "outbox"

    def behavior(run_no, kw):
        (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
        raise RuntimeError("handoff write failed after the signal")

    journal, prepare = _mock_pipeline(
        monkeypatch, "401 Invalid credential\n", behavior,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1, "a crash is not retried"
    assert result[2] == 1
    assert not (outbox / "BLOCKED-TODO-0001.md").exists(), (
        "the signal came first — the crash must not block the unit"
    )

    from awf.pipeline_state import read_state

    assert read_state(project).get("last_signal") is None


def test_transient_death_gets_the_net_retry(project, tmp_path, monkeypatch, capsys):
    """AC31: HTTP 500 → network retry; the silent counter is untouched."""
    def behavior(run_no, kw):
        if run_no >= 2:
            outbox = project / ".agentic" / "outbox"
            (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
            (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    journal, prepare = _mock_pipeline(
        monkeypatch, "HTTP 500 Internal Server Error\n", behavior,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert result[2] == 0
    assert journal["runs"] == 2, "one backoff retry, then the successful run"
    assert all("RETRY" not in (note or "") for note in journal["retry_notes"])

    from awf.pipeline_state import read_state

    assert "network failure — retrying" in capsys.readouterr().err, (
        "the retry must be the network backoff, not a silent one"
    )
    assert read_state(project).get("sw_retry_count") in (None, 0), (
        "the 5xx retry must not spend the silent budget"
    )


def test_bare_numbers_do_not_trigger_the_net_retry(project, tmp_path, monkeypatch):
    """AC32: '1500 passed' / 'took 429ms' stay silent retries."""
    journal, prepare = _mock_pipeline(
        monkeypatch, "1500 passed\ntook 429ms\n", lambda run_no, kw: None,
    )
    prepare(tmp_path)

    _run_stage(project, tmp_path)

    assert journal["runs"] == 3, "silent budget, not the net budget"
    assert any("RETRY" in (note or "") for note in journal["retry_notes"])


def test_auth_stop_without_a_note_never_claims_blocked(project, tmp_path, monkeypatch):
    """AUD5: a refused closure write is a salvage stop.

    ``last_signal=BLOCKED`` without a readable note would tell the
    supervisor the unit is blocked while ``awf blocked-draft`` has nothing
    to read. The stop must stay typed and the state honest.
    """
    from awf.infra import managed_fs
    from awf.pipeline_state import read_state

    real = managed_fs.managed_write_text

    def refuse_blocked(path, text, *, root):
        if Path(path).name.startswith("BLOCKED-"):
            raise managed_fs.ManagedPathError("BLOCKED closure refused")
        return real(path, text, root=root)

    monkeypatch.setattr(managed_fs, "managed_write_text", refuse_blocked)
    journal, prepare = _mock_pipeline(
        monkeypatch, "401 Invalid credential\n", lambda run_no, kw: None,
    )
    prepare(tmp_path)

    _run_stage(project, tmp_path)

    assert journal["runs"] == 1
    state = read_state(project)
    assert state.get("last_signal") is None
    assert state.get("salvage_needed") is True
    assert not (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").exists()
    assert (project / ".agentic" / "inbox" / "SALVAGE-TODO-0001.md").is_file()


# ── PA1/PA2/PA3 (REVIEW 0276, виток 2) ───────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # AUD1 positives: the code in an HTTP/status context or at line start,
        # AND the credential phrase K12 requires.
        ("HTTP 401 Invalid credential", "auth"),
        ("statusCode: 403 invalid api key", "auth"),
        ("401 Invalid credential", "auth"),
        ("HTTP 401 unauthorized", "auth"),
        ("403 Forbidden: invalid_api_key", "auth"),
        # AUD1 negatives: a status code without a credential phrase is a
        # rejected request, not a dead key.
        ("HTTP 401", ""),
        ("HTTP/1.1 401", ""),
        ("GET /login -> HTTP 401", ""),
        ("statusCode: 401", ""),
        ("code: 403", ""),
        # the exact failing-auth-test line — a bare ``assert 401`` under a
        # test whose NAME says unauthorized is project output.
        (
            "FAILED tests/test_auth.py::test_unauthorized_access_is_rejected"
            " - assert 401 == 200",
            "",
        ),
        # a bare code with no HTTP/status context and no auth phrase
        ("assert 401 == 200\nunauthorized", ""),
        # URL path, not a status
        ("GET http://host/401/unauthorized", ""),
    ],
)
def test_auth_needs_a_code_and_a_credential_phrase(text, expected):
    assert _net.classify_failure(text) == expected


def test_oom_rc_outranks_an_auth_string():
    """PA1: rc 137 / -9 is a memory kill — the log text cannot make it auth."""
    assert classify_worker_exit(137, "401 Invalid credential") == "oom-like"
    assert classify_worker_exit(-9, "HTTP 401 unauthorized") == "oom-like"
    assert classify_worker_exit(0, "Killed\n401 Invalid credential") == "oom-like"
    # a normal exit still classifies by the text
    assert classify_worker_exit(1, "401 Invalid credential") == "auth"
    # D11: a network marker on another line must not revive the auth text —
    # only a BARE marker keeps its retry precedence (test_worker_exit).
    assert _net.classify_failure(
        "Cannot connect to API\nAI_APICallError: unauthorized", 137
    ) == ""
    assert classify_worker_exit(
        137, "Cannot connect to API\nAI_APICallError: unauthorized"
    ) == "oom-like"


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 500",
        "HTTP 500 Internal Server Error",
        "503 Service Unavailable",
        "429 Too Many Requests",
        "overloaded",
    ],
)
def test_oom_rc_outranks_a_five_xx_text(text):
    """AC31/D11 (F6): a memory kill plus 5xx text is still ``oom-like`` —
    WIP 0276 protected OOM only from auth, so the 5xx text stayed network."""
    assert _net.classify_failure(text, 137) == ""
    assert classify_worker_exit(137, text) == "oom-like"
    assert _net.classify_failure(text, None) in _net.RETRY_FAILURE_CLASSES


def test_auth_outranks_a_network_marker_when_no_5xx_is_present():
    """R12: auth is before network — a dead key stops the stage even if the
    same tail also carries a reset (only D6, a 5xx/429, flips it)."""
    assert _net.classify_failure(
        "AI_APICallError: Connection reset\nAI_APICallError: unauthorized"
    ) == "auth"
    assert _net.classify_failure(
        "AI_APICallError: Connection reset\nAI_APICallError: unauthorized\nHTTP 503"
    ) == "network"


@pytest.mark.parametrize(
    "text",
    [
        # code-only and keyed code inside the SECOND wrapper form
        "error.error= 403",
        "error.error=statusCode: 403",
        # ``forbidden`` is a K12 phrase on its own inside the wrapper
        "AI_APICallError: Forbidden",
        # JSON dump: quoted key + phrase
        '{"statusCode": 401, "message": "Unauthorized"}',
    ],
)
def test_provider_context_accepts_code_or_phrase(text):
    """K12: inside the provider wrapper a code OR a phrase is enough. The OR
    branch must hold for BOTH wrapper spellings and for ``forbidden`` — the
    matrix pins only the ``AI_APICallError: statusCode: 401`` example."""
    assert _net.classify_failure(text) == "auth"


def test_provider_context_is_line_scoped():
    """K12: the wrapper licenses code-or-phrase on ITS line only.

    A wrapper on one line must not turn a bare code on a later line into a
    provider refusal: the same project output (``statusCode: 401`` in a test
    dump) would then BLOCK the stage just because an ``AI_APICallError``
    appeared earlier in the tail."""
    assert _net.classify_failure(
        "AI_APICallError: API call failed\nstatusCode: 401"
    ) == ""


@pytest.mark.parametrize("rc", [137, -9])
def test_classifier_itself_applies_the_oom_rule(rc):
    """PA1 lives in ONE place: the engine calls ``classify_failure`` with the
    exit code, so it cannot reach BLOCKED where the diagnostic says oom."""
    from awf._net import is_oom_exit

    assert _net.classify_failure("HTTP 401 unauthorized", rc) == ""
    assert _net.classify_failure("HTTP 401 unauthorized", None) == "auth"
    assert _net.classify_failure("HTTP 401 unauthorized", 1) == "auth"
    assert is_oom_exit(rc, "")
    assert _net.classify_failure("Killed\n401 Invalid credential", 1) == ""
    assert is_oom_exit(1, "Killed (out of memory)")
    assert not is_oom_exit(1, "no kill here")


def test_oom_crash_does_not_block_on_an_auth_string(project, tmp_path, monkeypatch):
    """PA1 at the ENGINE boundary: an rc-137 crash whose log quotes an auth
    line must not become a BLOCKED stop — the diagnostic line says oom-like,
    so the engine must agree (the exit code comes from the log holder)."""
    from awf.pipeline_state import read_state

    def behavior(run_no, kw):
        holder = kw.get("log_holder")
        if holder is not None:
            holder["exit_code"] = "137"
        raise RuntimeError("agent stage crashed")

    journal, prepare = _mock_pipeline(
        monkeypatch, "HTTP 401 unauthorized\n", behavior,
    )
    prepare(tmp_path)

    result = _run_stage(project, tmp_path)

    assert journal["runs"] == 1
    assert result[2] == 1
    assert not (project / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").exists(), (
        "an rc-137 death must not block on its own log text"
    )
    assert read_state(project).get("last_signal") is None


def test_signal_name_is_normalized_for_both_readers(tmp_path):
    """PA2: the watcher file name and the outbox reader print one name."""
    from awf.signals import read_signal_for_todo

    outbox = tmp_path / "outbox"
    outbox.mkdir()
    (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
    (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")

    from_reader = read_signal_for_todo(outbox, "TODO-0001", "DONE")
    from_watcher_file = "DONE-TODO-0001.ready"

    assert from_reader == "DONE-TODO-0001"
    assert signal_present_line(from_watcher_file) == signal_present_line(from_reader)
    assert signal_present_line(from_watcher_file) == "U6b: signal present (DONE-TODO-0001)"
    # the BD-21 typo form (``.md.ready``) normalizes to the same name
    assert signal_present_line("DONE-TODO-0001.md.ready") == (
        "U6b: signal present (DONE-TODO-0001)"
    )


def test_engine_and_closer_blocked_notes_share_the_schema(tmp_path):
    """PA3: two writers (engine auth, closer) — one schema.

    If the schema changes in one writer only, the supervisor sees a note
    ``awf blocked-draft`` cannot parse. The headings are compared, not the
    wording: the verdict and findings differ by producer by design.
    """
    from awf import closer_stage

    def headings(text: str) -> list[str]:
        return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("## ")]

    engine_proj = tmp_path / "engine"
    (engine_proj / ".agentic" / "outbox").mkdir(parents=True)
    engine_proj.joinpath(".agentic", "logs").mkdir()
    pipeline_engine._write_blocked_signal_note(
        engine_proj, "TODO-0001", "worker", "reason", "401 Invalid credential",
    )

    closer_proj = tmp_path / "closer"
    (closer_proj / ".agentic" / "outbox").mkdir(parents=True)
    (closer_proj / ".agentic" / "logs").mkdir()
    closer_stage._blocked_note(
        closer_proj, "TODO-0001",
        verdict_line="blocked", findings=["f"], directions=["d"],
    )

    engine_note = (engine_proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    closer_note = (closer_proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.md").read_text(
        encoding="utf-8"
    )

    assert headings(engine_note) == headings(closer_note)
    assert headings(engine_note) == [
        "## ВЕРДИКТ", "## НАХОДКИ", "## НАПРАВЛЕНИЯ-ФИКСА", "## ПЕРЕНОСЫ",
    ]
    for proj in (engine_proj, closer_proj):
        assert (proj / ".agentic" / "outbox" / "BLOCKED-TODO-0001.ready").is_file()
