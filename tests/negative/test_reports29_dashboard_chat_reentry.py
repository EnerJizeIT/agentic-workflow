"""REPORTS29: dashboard Agent Chat after a stage re-entry (owner, 09.10).

After continue/replan (verify → implementer back) the chat looked broken:
two entries of one role, the FINISHED handoff of the PREVIOUS attempt
borrowed the NEW attempt's span times (both "17:46:41"), the QA entry had
empty times, and the "who → whom" direction was invisible.

Root cause: spans were keyed by stage NAME over the CURRENT run's
transitions only — a re-entered stage's earlier attempts (rollback/replan,
kill+continue) were gone, and the last occurrence won. Now:
1. every stage occurrence is an attempt with its OWN span (cross-run
   ``all_transitions``), the handoff file's mtime picks its attempt;
2. a re-entry is marked visibly: «возврат: <откуда> → <куда> (попытка N)»,
   plain handoffs show «от → к»;
3. no span data → an honest "—", not an empty string.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awf import api
from awf.api.dashboard import generate_state_dict
from awf.pipeline_state import write_state


def _utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def dash_project(tmp_git_repo: Path) -> Path:
    """Project with .agentic/ + the 4-stage default pipeline."""
    api.init_project(tmp_git_repo, project_name="ReentryDash")
    pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
    pipes_dir.mkdir(parents=True, exist_ok=True)
    (pipes_dir / "default.yaml").write_text(
        "stages:\n"
        "  - name: plan\n    role: supervisor\n"
        "  - name: agent-implementer\n    role: agent-implementer\n"
        "  - name: agent-qa-review\n    role: agent-qa-review\n"
        "  - name: verify\n    role: supervisor\n",
        encoding="utf-8",
    )
    return tmp_git_repo


def _write_log(proj: Path, lines: list[str]) -> None:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "orchestrator.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_handoff(proj: Path, stage: str, todo: str, mtime: float) -> None:
    d = proj / ".agentic" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{stage}-{todo}.md"
    f.write_text(f"# {stage}\n\ndone\n", encoding="utf-8")
    os.utime(f, (mtime, mtime))


class TestReentryTimes:
    """(a) two attempts of one role: finished handoff keeps its OWN times."""

    def test_replan_finished_handoff_keeps_first_attempt_times(self, dash_project):
        # One run, rollback: verify sends implementer back (replan logs no
        # 'Stage' line, so the sequence is plan → impl → qa → verify → impl).
        _write_log(dash_project, [
            "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
            "[2026-10-09T17:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        # Handoffs written at the END of each attempt (before the next
        # 'Stage N:' line) — the mtime is inside the attempt's window.
        _write_handoff(dash_project, "agent-implementer", "TODO-0001", _utc(2026, 10, 9, 17, 14))
        _write_handoff(dash_project, "agent-qa-review", "TODO-0001", _utc(2026, 10, 9, 17, 24))
        write_state(
            dash_project, stage_idx=1, stage_name="agent-implementer",
            stage_kind="execute", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        chat = d["handoffs"]

        active = chat[0]
        assert active["active"] is True
        assert active["role"] == "agent-implementer"
        assert active["started_epoch"] == _utc(2026, 10, 9, 17, 40)

        done_impl = next(c for c in chat if not c["active"] and c["role"] == "agent-implementer")
        done_qa = next(c for c in chat if not c["active"] and c["role"] == "agent-qa-review")

        # The finished handoff keeps the FIRST attempt's own times — not the
        # new attempt's span (both "17:46:41" was the bug).
        assert done_impl["started_epoch"] == _utc(2026, 10, 9, 17, 5)
        assert done_impl["started_epoch"] != active["started_epoch"]
        assert done_impl["started_at"] != active["started_at"]
        assert done_impl["ended_at"] and done_impl["ended_at"] != active["started_at"]
        assert done_impl["duration"] == "10m 0s"

        # The QA entry is NOT empty (owner: «у QA — пусто»).
        assert done_qa["started_epoch"] == _utc(2026, 10, 9, 17, 15)
        assert done_qa["ended_at"]  # 17:25 local clock

        # Newest first: active (17:40) on top, then QA (17:15), then the
        # first implementer attempt (17:05).
        assert [c["role"] for c in chat] == [
            "agent-implementer", "agent-qa-review", "agent-implementer",
        ]

    def test_resume_after_kill_keeps_previous_run_times(self, dash_project):
        # kill+continue: run 1 died in implementer, run 2 restarts it. The
        # finished handoff (run 1) must not borrow the run-2 attempt's span.
        _write_log(dash_project, [
            "[2026-10-09T12:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T12:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T12:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T12:20:00Z] Pipeline stopped at stage agent-implementer: watchdog",
            "[2026-10-09T12:40:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T12:40:00Z] Starting from stage: agent-implementer (index 1)",
            "[2026-10-09T12:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        _write_handoff(dash_project, "agent-implementer", "TODO-0002", _utc(2026, 10, 9, 12, 19))
        write_state(
            dash_project, stage_idx=1, stage_name="agent-implementer",
            stage_kind="execute", todo_id="TODO-0002",
        )

        d = generate_state_dict(dash_project)
        chat = d["handoffs"]

        active = chat[0]
        done = next(c for c in chat if not c["active"])
        assert active["started_epoch"] == _utc(2026, 10, 9, 12, 40)
        # Run-1 handoff: its own start (12:05), distinct from the run-2 one.
        assert done["started_epoch"] == _utc(2026, 10, 9, 12, 5)
        assert done["started_epoch"] != active["started_epoch"]
        assert done["started_at"] and done["started_at"] != active["started_at"]


class TestReentryMark:
    """(b) the «возврат: … → …» label is present in the replan scenario."""

    def test_replan_active_entry_shows_return_mark(self, dash_project):
        _write_log(dash_project, [
            "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
            "[2026-10-09T17:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        _write_handoff(dash_project, "agent-implementer", "TODO-0001", _utc(2026, 10, 9, 17, 14))
        _write_handoff(dash_project, "agent-qa-review", "TODO-0001", _utc(2026, 10, 9, 17, 24))
        write_state(
            dash_project, stage_idx=1, stage_name="agent-implementer",
            stage_kind="execute", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        active = d["handoffs"][0]

        # The return is visible: verify sent implementer back, attempt 2.
        assert "возврат:" in active["direction"]
        assert "→" in active["direction"]
        assert "попытка 2" in active["direction"]
        # <откуда> is the stage that sent it back (verify, via its label).
        assert "Supervisor" in active["direction"]

    def test_same_stage_retry_is_repeat_not_return(self, dash_project):
        # The stage died and the engine restarted the SAME stage: «попытка 2»
        # without a direction (a repeat, not a return from another stage).
        _write_log(dash_project, [
            "[2026-10-09T12:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T12:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T12:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T12:20:00Z] Pipeline stopped at stage agent-implementer: watchdog",
            "[2026-10-09T12:40:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T12:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        write_state(
            dash_project, stage_idx=1, stage_name="agent-implementer",
            stage_kind="execute", todo_id="TODO-0002",
        )

        d = generate_state_dict(dash_project)
        active = d["handoffs"][0]
        assert "возврат" not in active["direction"]
        assert "попытка 2" in active["direction"]


class TestPlainChainDirection:
    """(c) plain chain: directions visible, times unchanged (no regression)."""

    def test_implementer_qa_verify_directions(self, dash_project):
        _write_log(dash_project, [
            "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
        ])
        _write_handoff(dash_project, "agent-implementer", "TODO-0003", _utc(2026, 10, 9, 17, 14))
        _write_handoff(dash_project, "agent-qa-review", "TODO-0003", _utc(2026, 10, 9, 17, 24))
        write_state(
            dash_project, stage_idx=3, stage_name="verify",
            stage_kind="verify", todo_id="TODO-0003",
        )

        d = generate_state_dict(dash_project)
        chat = d["handoffs"]
        by = {(c["role"], c["active"]): c for c in chat}

        # Each handoff shows where the baton goes.
        assert by[("agent-implementer", False)]["direction"] == "Implementer → QA Review"
        assert by[("agent-qa-review", False)]["direction"] == "QA Review → Supervisor"
        assert by[("verify", True)]["direction"] == "Supervisor → конец"

        # Times/durations are the pre-report behavior (no regression).
        impl = by[("agent-implementer", False)]
        assert impl["started_epoch"] == _utc(2026, 10, 9, 17, 5)
        assert impl["duration"] == "10m 0s"
        assert impl["attempt"] == 1
        qa = by[("agent-qa-review", False)]
        assert qa["started_epoch"] == _utc(2026, 10, 9, 17, 15)
        assert qa["duration"] == "10m 0s"


class TestNoSpanData:
    """(invariant 3) no span data for the handoff → an honest '—', not ''."""

    def test_handoff_without_log_data_shows_dash(self, dash_project):
        # The handoff exists but the log has NO stage line for it (rotated
        # away) — the old code silently showed empty times.
        _write_log(dash_project, [
            "[2026-10-09T17:25:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
        ])
        _write_handoff(dash_project, "agent-qa-review", "TODO-0004", _utc(2026, 10, 9, 16, 24))
        write_state(
            dash_project, stage_idx=3, stage_name="verify",
            stage_kind="verify", todo_id="TODO-0004",
        )

        d = generate_state_dict(dash_project)
        chat = d["handoffs"]
        qa = next(c for c in chat if c["role"] == "agent-qa-review")
        assert qa["started_at"] == "—"
        assert qa["ended_at"] == "—"
        assert qa["duration"] == ""
        assert qa["started_epoch"] == 0
