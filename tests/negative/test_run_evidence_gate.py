"""AUD11-03: the run-mode (забег) evidence gate.

Incident: in an active run the verify stage accepted a file-based
ACK-{todo}.ready with NO independent-verification evidence — the exact
artifact run mode exists to guarantee (context/RUN-EVIDENCE-{todo}.md,
written by awf_approve(evidence=...)). All prompt surfaces taught the
file-based ACK, so the gate the run protocol promised was bypassed by
the instructions themselves (FU-16, live observation).

Fix under test: inside an ACTIVE run, ACK/APPROVE signals are ignored
until the evidence file exists. Outside a run, file-based ACK stays the
normal interactive flow (must not regress). REVIEW is never gated —
rejection needs no evidence.
"""
from __future__ import annotations

import pytest

from awf import run_state
from awf.supervisor import (
    _detect_supervisor_signal,
    _run_evidence_ok,
    wait_for_supervisor_signal,
)

T = "TODO-0009"


def _project(tmp_path):
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context", "state"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    return proj


def _start_run(proj):
    run_state.write_run(
        proj,
        active=True,
        queue=[T],
        started_at=run_state.now_iso(),
    )


def _evidence(proj, todo_id=T):
    (proj / ".agentic" / "context" / f"RUN-EVIDENCE-{todo_id}.md").write_text(
        "pytest -q → 348 passed; verdict: approve\n", encoding="utf-8"
    )


def _ack(proj, todo_id=T):
    (proj / ".agentic" / "inbox" / f"ACK-{todo_id}.ready").write_text("", encoding="utf-8")


def _approve_file(proj, todo_id=T):
    (proj / ".agentic" / "inbox" / f"APPROVE-{todo_id}.ready").write_text("", encoding="utf-8")


class TestVerifyWaitEvidenceGate:
    """wait_for_supervisor_signal — the interactive verify wait (auto=False)."""

    def test_ack_without_evidence_blocked_in_active_run(self, tmp_path, monkeypatch):
        """RED before fix: a fresh ACK in an active run satisfied verify."""
        proj = _project(tmp_path)
        _start_run(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                _ack(proj)

        monkeypatch.setattr("time.sleep", fake_sleep)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "verify", T, proj, proj / ".agentic" / "logs", timeout=1,
            )

    def test_ack_with_evidence_accepted_in_active_run(self, tmp_path, monkeypatch):
        proj = _project(tmp_path)
        _start_run(proj)
        _evidence(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                _ack(proj)

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", T, proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == f"ACK-{T}"

    def test_ack_without_evidence_ok_outside_run(self, tmp_path, monkeypatch):
        """Interactive (non-run) flow keeps working: ACK = approve."""
        proj = _project(tmp_path)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                _ack(proj)

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", T, proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == f"ACK-{T}"

    def test_evidence_written_mid_wait_unblocks(self, tmp_path, monkeypatch):
        """awf_approve(evidence=...) writes evidence + APPROVE — the gate
        must accept the APPROVE as soon as both exist."""
        proj = _project(tmp_path)
        _start_run(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            written["n"] += 1
            if written["n"] == 1:
                _evidence(proj)
                _approve_file(proj)

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", T, proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == f"APPROVE-{T}"

    def test_review_never_gated(self, tmp_path, monkeypatch):
        """Rejection needs no evidence — REVIEW is accepted in an active run."""
        proj = _project(tmp_path)
        _start_run(proj)
        outbox = proj / ".agentic" / "outbox"
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                (outbox / f"REVIEW-{T}.md").write_text("reject: X\n", encoding="utf-8")

        monkeypatch.setattr("time.sleep", fake_sleep)

        sig = wait_for_supervisor_signal(
            "verify", T, proj, proj / ".agentic" / "logs", timeout=5,
        )
        assert sig == f"REVIEW-{T}"

    def test_salvage_ack_without_evidence_blocked_in_active_run(self, tmp_path, monkeypatch):
        """Salvage approve is also an approve: in an active run the file-based
        ACK is gated here too (the wait branch covers verify/salvage/replan).
        Pins the scope — narrowing the gate to verify-only would hang the
        run's salvage wait for the full supervisor timeout."""
        proj = _project(tmp_path)
        _start_run(proj)
        written = {"n": 0}

        def fake_sleep(*_a, **_kw):
            if written["n"] == 0:
                written["n"] = 1
                _ack(proj)

        monkeypatch.setattr("time.sleep", fake_sleep)

        with pytest.raises(TimeoutError):
            wait_for_supervisor_signal(
                "salvage", T, proj, proj / ".agentic" / "logs", timeout=1,
            )


class TestFallbackDetectionEvidenceGate:
    """Auto-mode fallback (_detect_supervisor_signal) applies the same gate."""

    def test_ack_without_evidence_not_detected_in_active_run(self, tmp_path):
        proj = _project(tmp_path)
        _start_run(proj)
        _ack(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"

        assert _run_evidence_ok(proj, T) is False
        sig = _detect_supervisor_signal(
            "verify", T, inbox, outbox,
            run_evidence_ok=_run_evidence_ok(proj, T),
        )
        assert sig == ""

    def test_ack_with_evidence_detected_in_active_run(self, tmp_path):
        proj = _project(tmp_path)
        _start_run(proj)
        _evidence(proj)
        _ack(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"

        assert _run_evidence_ok(proj, T) is True
        sig = _detect_supervisor_signal(
            "verify", T, inbox, outbox,
            run_evidence_ok=_run_evidence_ok(proj, T),
        )
        assert sig == f"ACK-{T}"

    def test_ack_ok_outside_run(self, tmp_path):
        proj = _project(tmp_path)
        _ack(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"

        assert _run_evidence_ok(proj, T) is True
        sig = _detect_supervisor_signal(
            "verify", T, inbox, outbox,
            run_evidence_ok=_run_evidence_ok(proj, T),
        )
        assert sig == f"ACK-{T}"

    def test_review_never_gated(self, tmp_path):
        proj = _project(tmp_path)
        _start_run(proj)
        inbox = proj / ".agentic" / "inbox"
        outbox = proj / ".agentic" / "outbox"
        (outbox / f"REVIEW-{T}.md").write_text("reject: X\n", encoding="utf-8")

        sig = _detect_supervisor_signal(
            "verify", T, inbox, outbox,
            run_evidence_ok=_run_evidence_ok(proj, T),
        )
        assert sig == f"REVIEW-{T}"
