"""Test: salvage → approve → continue flow (critical regression guard).

Tests the exact scenario that was broken before fix:
1. Worker doesn't signal → salvage path starts
2. Supervisor creates APPROVE signal
3. Salvage detects APPROVE (was: kind=='verify' excluded salvage)
4. Pipeline continues

Without this test, the one-word fix (kind in ('verify','salvage'))
could silently regress.
"""
from __future__ import annotations

import os
import time

import pytest

from awf import api
from awf.supervisor import wait_for_supervisor_signal


@pytest.fixture
def project(tmp_git_repo):
    """Project with .agentic/ + pipeline + active TODO."""
    api.init_project(tmp_git_repo, project_name="Test")

    pipes = tmp_git_repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "stages:\n"
        '  - name: plan\n    role: supervisor\n'
        '  - name: worker\n    role: worker\n'
        '  - name: verify\n    role: supervisor\n',
        encoding="utf-8",
    )

    roles = tmp_git_repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute TODO.\n", encoding="utf-8")

    inbox = tmp_git_repo / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "TODO-0001.md").write_text("# Task\n")
    (inbox / "TODO-0001.ready").write_text("")

    logs_dir = tmp_git_repo / ".agentic" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    return tmp_git_repo


class TestSalvageSignalDetection:
    """Critical: salvage kind must detect ACK/APPROVE signals."""

    def test_salvage_detects_approve(self, project):
        """salvage wait_for_supervisor_signal finds APPROVE signal.

        Before fix: kind=='verify' excluded salvage → signal invisible.
        After fix: kind in ('verify','salvage') → signal detected.
        """
        inbox = project / ".agentic" / "inbox"
        todo_id = "TODO-0001"

        # Simulate supervisor creating APPROVE signal
        (inbox / f"APPROVE-{todo_id}.ready").write_text("")

        logs_dir = project / ".agentic" / "logs"

        # wait_for_supervisor_signal with kind="salvage" should find it
        # Use very short timeout — signal already exists, should return immediately
        result = wait_for_supervisor_signal(
            kind="salvage",
            todo_id=todo_id,
            project_dir=project,
            logs_dir=logs_dir,
            poll_interval=0,
            timeout=2,
        )

        assert result == f"APPROVE-{todo_id}", (
            f"Salvage should detect APPROVE signal. Got: {result}. "
            f"Check: kind in ('verify', 'salvage') in wait_for_supervisor_signal."
        )

    def test_salvage_detects_ack(self, project):
        """salvage wait_for_supervisor_signal finds ACK signal."""
        inbox = project / ".agentic" / "inbox"
        todo_id = "TODO-0001"

        (inbox / f"ACK-{todo_id}.ready").write_text("")

        result = wait_for_supervisor_signal(
            kind="salvage",
            todo_id=todo_id,
            project_dir=project,
            logs_dir=project / ".agentic" / "logs",
            poll_interval=0,
            timeout=2,
        )

        assert result == f"ACK-{todo_id}"

    def test_salvage_detects_review(self, project):
        """salvage wait_for_supervisor_signal finds REVIEW in outbox."""
        todo_id = "TODO-0001"
        outbox = project / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"REVIEW-{todo_id}.md").write_text("# Issues\nfix needed\n")

        result = wait_for_supervisor_signal(
            kind="salvage",
            todo_id=todo_id,
            project_dir=project,
            logs_dir=project / ".agentic" / "logs",
            poll_interval=0,
            timeout=2,
        )

        assert result == f"REVIEW-{todo_id}"

    def test_salvage_times_out_without_signal(self, project):
        """No signal → TimeoutError after timeout."""
        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                kind="salvage",
                todo_id="TODO-0001",
                project_dir=project,
                logs_dir=project / ".agentic" / "logs",
                poll_interval=0,
                timeout=1,
            )

    def test_verify_still_works_after_salvage_fix(self, project, monkeypatch):
        """Regression: verify kind still detects signals (not broken by salvage fix).

        TODO-0068: the signal is written on the first poll iteration (time.sleep
        hook, pattern from tests/negative/test_run_evidence_gate.py) — i.e.
        strictly after the wait's wall_start, not pre-written.

        TODO-0167: both operands of the AUD04-04 freshness compare
        (``int(st_mtime) >= int(wall_start)`` in _decision_signal_fresh) live
        on ONE controlled timeline. The two operands are independent time
        sources (kernel mtime vs the process wall clock read at wait start);
        on a CI VM a backward wall-clock step (NTP / pause-resume) inside the
        wait makes a signal written DURING the wait read stale, and its mtime
        never changes — so it stays stale for the whole wait and the call
        times out (the 08.10 CI flake: red once, green on retry). With
        ``time.time`` patched to a synthetic clock that only advances in the
        sleep hook, and the signal's mtime pinned to that same clock via
        os.utime (T0+3s, comfortably fresh at whole-second resolution), the
        compare cannot lose to wall-clock behavior. The gate itself runs
        unpatched — the accept path stays pinned; the stale-reject half is
        pinned by tests/negative/test_reports26_b5_restart_recovery.py.
        """
        inbox = project / ".agentic" / "inbox"
        todo_id = "TODO-0001"
        written = {"n": 0}
        wall = {"now": time.time()}

        def fake_time() -> float:
            return wall["now"]

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                # The signal appears 3s into the wait on the synthetic
                # timeline: strictly after wall_start, whole-second fresh.
                wall["now"] += 3
                sig = inbox / f"APPROVE-{todo_id}.ready"
                sig.write_text("", encoding="utf-8")
                os.utime(sig, (wall["now"], wall["now"]))

        monkeypatch.setattr("time.time", fake_time)
        monkeypatch.setattr("time.sleep", fake_sleep)

        result = wait_for_supervisor_signal(
            kind="verify",
            todo_id=todo_id,
            project_dir=project,
            logs_dir=project / ".agentic" / "logs",
            poll_interval=0,
            timeout=10,
        )

        assert result == f"APPROVE-{todo_id}"
