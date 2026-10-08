"""B5 (reports-26): restart recovery of a pipeline parked at verify.

Incident (awf-bug-20261008-mcp-obryv-ubil-pipeline-i-tuly.md + live
reproduction 2026-10-08): an opencode restart KILLS a pipeline parked at
the verify wait — cgroup termination takes down the setsid-detached
process too, and the MCP tools are gone until the restart. Recovery
worked manually: `awf continue --from-stage verify` re-launched the
stage, the FIRST approve (written before the new wait started) was
dropped as stale by AUD04-04, and the RE-approve closed the cycle.

This suite pins that recovery at the engine-primitive level
(``wait_for_supervisor_signal`` + ``_consume_verify_decision``):

- (a) an APPROVE written BEFORE the verify wait started is discarded —
  the stage keeps waiting (TimeoutError, stale line in the log);
- (b) the missing half — stale→fresh in ONE wait: the leftover is
  ignored, a re-approval written DURING the wait is accepted and the
  cycle closes; the accepted decision is then consumed, so the next
  verify cannot re-approve on a dead approval.

Single-stale half already covered: test_audit12_incidents.py
(TestStaleApprove) and test_escalation_recovery.py (ACK twins for
kind="replan").
"""
from __future__ import annotations

import os
import time

import pytest

import awf.pipeline_engine as engine
from awf.supervisor import wait_for_supervisor_signal

TODO_ID = "TODO-0155"


def _project(tmp_path):
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context", "state", "handoff"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    return proj


def _stale_approve(proj, age_seconds: float = 3600.0):
    """APPROVE left on disk by the pre-restart cycle (old mtime)."""
    sig = proj / ".agentic" / "inbox" / f"APPROVE-{TODO_ID}.ready"
    sig.write_text("", encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(sig, (old, old))
    return sig


class TestRestartRecoveryWait:
    def test_approve_written_before_wait_is_discarded(self, tmp_path, monkeypatch):
        """(a) stale APPROVE must not close the new verify wait.

        The file survived the kill (the dead process never consumed it) —
        accepting it by existence would auto-commit on a dead approval.
        Complements test_audit12_incidents.TestStaleApprove (same gate,
        here as the restart-recovery sequence).
        """
        proj = _project(tmp_path)
        _stale_approve(proj)
        monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "verify", TODO_ID, proj, proj / ".agentic" / "logs", timeout=1,
            )

        log = (proj / ".agentic" / "logs" / "orchestrator.log").read_text(
            encoding="utf-8",
        )
        assert "AUD04-04: stale decision signal ignored: APPROVE-" in log

    def test_stale_then_fresh_approve_closes_single_wait(self, tmp_path, monkeypatch):
        """(b) stale→fresh in ONE wait: re-approve after restart wins.

        The pre-restart APPROVE is still on disk when `awf continue
        --from-stage verify` starts the new wait — it must be ignored,
        and the supervisor's re-approval (fresh mtime, written during
        the wait) must be the decision that closes the cycle.
        """
        proj = _project(tmp_path)
        sig_path = _stale_approve(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                # the supervisor re-approves after the restart: the same
                # signal file, a fresh mtime
                sig_path.write_text("", encoding="utf-8")

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", TODO_ID, proj, proj / ".agentic" / "logs", timeout=10,
        )
        assert sig == f"APPROVE-{TODO_ID}", (
            f"the re-approval must close the wait, got {sig!r}"
        )

    def test_accepted_decision_is_consumed_and_cycle_closes(self, tmp_path, monkeypatch):
        """The fresh approve is consumed once the cycle acted on it.

        Without consumption the accepted file survives; the next verify
        of the same TODO (another restart) would see it again. After
        ``_consume_verify_decision`` the signal is gone and the
        acceptance record is cleared — a future re-approval must be a
        genuinely fresh file.
        """
        proj = _project(tmp_path)
        sig_path = _stale_approve(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                sig_path.write_text("", encoding="utf-8")

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", TODO_ID, proj, proj / ".agentic" / "logs", timeout=10,
        )
        assert sig == f"APPROVE-{TODO_ID}"

        engine._consume_verify_decision(proj, TODO_ID, sig, proj / ".agentic" / "logs")
        assert not sig_path.exists(), (
            "the accepted decision must not survive the cycle that acted on it"
        )
