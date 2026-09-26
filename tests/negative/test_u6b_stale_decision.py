"""U6b Part B: the auto-verify fallback must not accept a stale decision.

Compound-failure window (QA flag from TODO-0014): verify ACCEPTS
APPROVE/ACK/REVIEW, then the pipeline is killed before the decision is
consumed — the decision file is left behind. On kill+continue a fresh
verify runs; its fallback detector (supervisor._detect_supervisor_signal)
used to accept the leftover by mere existence → auto-commit on a dead
approval.

Fix: the engine records the accepted decision (signal name + file mtime at
acceptance) in the pipeline state and clears the record at consumption.
The fallback rejects a decision file that matches the already-accepted
record at the same or older mtime (the same physical file). A newer mtime
is a fresh re-approval and stays accepted.

BD-8 pre-approval (APPROVE created BEFORE the run) has no record — it must
keep being accepted by the first verify.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from awf import commit_plan, pipeline_engine, supervisor
from awf.pipeline_state import read_state


def _bare_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for d in ("inbox", "outbox", "logs", "roles"):
        (proj / ".agentic" / d).mkdir(parents=True)
    (proj / ".agentic" / "roles" / "supervisor.md").write_text(
        "# Supervisor\n", encoding="utf-8"
    )
    return proj


def _fake_subprocess_no_signal(monkeypatch):
    """The supervisor subprocess exits cleanly and fires no watcher signal —
    exactly the kill+continue situation: nothing NEW appeared during this
    cycle, only the leftover from the previous one."""
    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None,
                 signal_holder=None, **kwargs):
        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )


class TestStaleDecisionGate:
    """Unit level: _detect_supervisor_signal with an acceptance record."""

    def _write_approve(self, proj: Path, mtime: float | None = None) -> Path:
        p = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
        p.write_text("", encoding="utf-8")
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_stale_approve_leftover_rejected(self, tmp_path):
        """The kill-between-acceptance-and-consumption window: the file is
        the SAME physical file (same mtime) as the one already accepted →
        the fallback must not re-accept it."""
        proj = _bare_project(tmp_path)
        approve = self._write_approve(proj)
        t0 = approve.stat().st_mtime

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0001",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
            accepted_decision="APPROVE-TODO-0001",
            accepted_decision_mtime=t0,
        )
        assert result == "", (
            f"stale APPROVE (same mtime as the accepted one) was re-accepted: {result!r}"
        )

    def test_fresh_reapproval_accepted(self, tmp_path):
        """The owner re-approves after the kill: the file got a NEW mtime —
        that is a fresh decision, not the leftover."""
        proj = _bare_project(tmp_path)
        approve = self._write_approve(proj)
        t0 = approve.stat().st_mtime
        # Re-approval a second later
        approve.write_text("", encoding="utf-8")
        t1 = t0 + 2
        os.utime(approve, (t1, t1))

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0001",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
            accepted_decision="APPROVE-TODO-0001",
            accepted_decision_mtime=t0,
        )
        assert result == "APPROVE-TODO-0001", (
            "a re-approval with a newer mtime must be accepted"
        )

    def test_preapproval_without_record_accepted(self, tmp_path):
        """BD-8 pre-approval: APPROVE exists before the run, no acceptance
        record — the first verify accepts it. (5 e2e tests rely on this.)"""
        proj = _bare_project(tmp_path)
        self._write_approve(proj)

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0001",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
        )
        assert result == "APPROVE-TODO-0001"

    def test_stale_review_leftover_rejected(self, tmp_path):
        """A leftover REVIEW from a killed cycle must not re-trigger replan."""
        proj = _bare_project(tmp_path)
        review = proj / ".agentic" / "outbox" / "REVIEW-TODO-0001.md"
        review.write_text("# Review\nstale\n", encoding="utf-8")
        t0 = review.stat().st_mtime

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0001",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
            accepted_decision="REVIEW-TODO-0001",
            accepted_decision_mtime=t0,
        )
        assert result == ""

    def test_stale_ack_leftover_rejected(self, tmp_path):
        proj = _bare_project(tmp_path)
        ack = proj / ".agentic" / "inbox" / "ACK-TODO-0001.ready"
        ack.write_text("", encoding="utf-8")
        t0 = ack.stat().st_mtime

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0001",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
            accepted_decision="ACK-TODO-0001",
            accepted_decision_mtime=t0,
        )
        assert result == ""

    def test_record_for_other_todo_does_not_block(self, tmp_path):
        """The record is per-signal: a leftover for TODO-0001 must not block
        a genuine decision for TODO-0002."""
        proj = _bare_project(tmp_path)
        approve2 = proj / ".agentic" / "inbox" / "APPROVE-TODO-0002.ready"
        approve2.write_text("", encoding="utf-8")
        t0 = time.time() - 3600  # record from a cycle ago, other TODO
        os.utime(approve2, (t0, t0))

        result = supervisor._detect_supervisor_signal(
            "verify", "TODO-0002",
            proj / ".agentic" / "inbox", proj / ".agentic" / "outbox",
            accepted_decision="APPROVE-TODO-0001",
            accepted_decision_mtime=t0,
        )
        assert result == "APPROVE-TODO-0002"


class TestStaleDecisionWiring:
    """The auto-verify path (run_supervisor_via_subprocess) must pass the
    acceptance record from the pipeline state into the fallback."""

    def test_auto_verify_rejects_stale_approve(self, tmp_path, monkeypatch):
        """Full window: cycle 1 accepted APPROVE (recorded), kill before
        consumption. Cycle 2: the supervisor subprocess fires nothing new —
        the leftover must NOT be accepted (→ no auto-commit)."""
        proj = _bare_project(tmp_path)
        logs = proj / ".agentic" / "logs"
        approve = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
        approve.write_text("", encoding="utf-8")

        # Cycle 1 acceptance — what the engine records:
        pipeline_engine._record_verify_decision(
            proj, "TODO-0001", "APPROVE-TODO-0001", logs
        )
        _fake_subprocess_no_signal(monkeypatch)

        signal = supervisor.run_supervisor_via_subprocess(
            "verify", "TODO-0001", proj, {}, "phases/plan.md", logs
        )
        assert signal == "", (
            f"stale APPROVE from the killed cycle was accepted again: {signal!r}"
        )

    def test_auto_verify_accepts_preapproval(self, tmp_path, monkeypatch):
        """BD-8: no record + APPROVE on disk (created before the run) →
        the auto-verify accepts it. This is the documented e2e pattern."""
        proj = _bare_project(tmp_path)
        logs = proj / ".agentic" / "logs"
        (proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").write_text(
            "", encoding="utf-8"
        )
        _fake_subprocess_no_signal(monkeypatch)

        signal = supervisor.run_supervisor_via_subprocess(
            "verify", "TODO-0001", proj, {}, "phases/plan.md", logs
        )
        assert signal == "APPROVE-TODO-0001"

    def test_consume_clears_record(self, tmp_path):
        """Normal cycle: acceptance is recorded, consumption clears both the
        file and the record — the next verify starts fresh."""
        proj = _bare_project(tmp_path)
        logs = proj / ".agentic" / "logs"
        approve = proj / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready"
        approve.write_text("", encoding="utf-8")

        pipeline_engine._record_verify_decision(
            proj, "TODO-0001", "APPROVE-TODO-0001", logs
        )
        state = read_state(proj)
        assert state["accepted_decision"] == "APPROVE-TODO-0001"
        assert state["accepted_decision_mtime"] > 0

        pipeline_engine._consume_verify_decision(
            proj, "TODO-0001", "APPROVE-TODO-0001", logs
        )
        assert not approve.exists(), "the decision file must be consumed"
        state = read_state(proj)
        assert state.get("accepted_decision") is None, (
            "the acceptance record must be cleared on consumption — a fresh "
            "re-approval must not be mistaken for this cycle's leftover"
        )


def _git_project(tmp_path: Path) -> Path:
    """Git repo + .agentic + pipeline (same shape as test_pipeline_e2e)."""
    from conftest import _git_init

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    from awf import api

    api.init_project(repo, project_name="Test")
    pipes = repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "worker"\n    role: "worker"\n'
        '    on_blocked: "escalate"\n    max_retries: 3\n'
        '  - name: "verify"\n    role: "supervisor"\n'
        '    on_approved: "commit_and_next"\n    on_rejected: "replan"\n',
        encoding="utf-8",
    )
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\n", encoding="utf-8")
    (roles / "supervisor.md").write_text("# Supervisor\n", encoding="utf-8")

    inbox = repo / ".agentic" / "inbox"
    (inbox / "TODO-0001.md").write_text("# TODO-0001\nTask\n", encoding="utf-8")
    (inbox / "TODO-0001.ready").write_text("", encoding="utf-8")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "BASELINE-TODO-0001.sha").write_text(sha + "\n", encoding="utf-8")
    return repo


class TestEngineRecordsAcceptance:
    def test_record_exists_before_commit_gate(self, tmp_path, monkeypatch):
        """The acceptance must be recorded BEFORE the commit gate runs — the
        kill window between acceptance and consumption contains the commit
        gate, so the record must already be in state when the gate starts.
        (Without it, a kill in that window leaves no trace and the leftover
        APPROVE is re-accepted on continue.)"""
        proj = _git_project(tmp_path)
        commit_gate_states: list[tuple[str, dict | None]] = []

        def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            if stage.kind == "plan":
                return ""
            if stage.kind == "verify":
                inbox_path = project_dir / ".agentic" / "inbox"
                (inbox_path / f"APPROVE-{todo_id}.ready").write_text("", encoding="utf-8")
                return f"APPROVE-{todo_id}"
            return ""

        def fake_maybe_commit(s_name, *a, **kw):
            commit_gate_states.append((s_name, read_state(proj)))
            # R-03: the gate returns a typed outcome — committed proceeds
            # (the fake does not commit anything; the state record is the
            # point of this test).
            return commit_plan.CommitOutcome(commit_plan.OUTCOME_COMMITTED, "", "fake")

        outbox = proj / ".agentic" / "outbox"

        def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
            (outbox / "DONE-TODO-0001.md").write_text("# Done\n", encoding="utf-8")
            (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
            (proj / "src.txt").write_text("worker output\n", encoding="utf-8")

        monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", mock_supervisor)
        monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
        monkeypatch.setattr(pipeline_engine, "_maybe_commit", fake_maybe_commit)

        from types import SimpleNamespace

        from awf.orchestrator import run_pipeline

        rc = run_pipeline(SimpleNamespace(
            project_dir=str(proj), pipeline=None, from_stage=None,
            auto=True, timeout=None,
        ))
        assert rc == 0, "the happy path must still complete"
        verify_gates = [st for name, st in commit_gate_states if name == "verify"]
        assert verify_gates, f"commit gate never ran at verify: {commit_gate_states}"
        at_gate = verify_gates[0]
        assert at_gate is not None and at_gate.get("accepted_decision") == "APPROVE-TODO-0001", (
            f"acceptance not recorded before the verify commit gate: {at_gate}"
        )
