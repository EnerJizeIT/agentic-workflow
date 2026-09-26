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
        strictly after the wait's wall_start. The old version pre-wrote the
        file, which raced the AUD04-04 whole-second freshness gate
        (_decision_signal_fresh): if the write and the call straddled a second
        boundary the just-written signal read stale and the wait timed out —
        a 2s TimeoutError flake under xdist -n auto, never standalone.
        timeout=10 is a safety margin only: the wait ends on the second
        poll iteration.
        """
        inbox = project / ".agentic" / "inbox"
        todo_id = "TODO-0001"
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                (inbox / f"APPROVE-{todo_id}.ready").write_text("", encoding="utf-8")

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
