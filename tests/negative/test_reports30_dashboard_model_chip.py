"""REPORTS30: the dashboard Agent Chat shows the model on every card.

Owner's backlog 09.10: «ты вроде сделал отображение моделей. А они не
отображаются» — 0175 put the model into the role files and added the
config cross-check, but the dashboard never showed it: the /api/state
handoff entries had no `model` field and the template had no chip.

Invariants (TODO-0185):
1. Chat entries carry `model`, sourced from the stage record's "Stage
   facts" (the handoff the engine writes after the attempt,
   ``- model: `<id>``` — ORCH M4.3):
   (a) stage facts `model: vllm/llm` → the entry has model="vllm/llm"
       and the card renders the chip;
   (b) no model line (pre-M4.3 handoff, role without a model) →
       model="" and the chip shows «—»;
   (c) two attempts — the model of the attempt whose window the file's
       mtime falls into (REPORTS29), not the other attempt's.
2. The active entry's model is "" — the current span has no stage
   record yet (the handoff is written at the stage's end); an honest
   «—», not a guess.
3. Existing entry keys are not broken (only `model` added); the
   attempt/return marks (0176) and the times are untouched.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from awf import api
from awf.api.dashboard import generate_dashboard, generate_state_dict
from awf.pipeline_state import write_state


def _utc(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


@pytest.fixture
def dash_project(tmp_git_repo: Path) -> Path:
    """Project with .agentic/ + the 4-stage default pipeline."""
    api.init_project(tmp_git_repo, project_name="ModelChipDash")
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


def _write_handoff(
    proj: Path, stage: str, todo: str, mtime: float, body: str,
) -> None:
    d = proj / ".agentic" / "handoff"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{stage}-{todo}.md"
    f.write_text(body, encoding="utf-8")
    os.utime(f, (mtime, mtime))


def _stage_facts(model: str = "", attempt: int = 1) -> str:
    """The handoff skeleton as the engine writes it (ORCH M4.3)."""
    lines = [
        "# Handoff from `agent-implementer` (TODO TODO-0001)",
        "",
        "## Run facts",
        "",
        "- generated: 2026-10-09T17:14:00Z",
        "- stage role: `agent-implementer`",
        "- worker run: 1",
        "",
        "## Stage facts",
        "",
        "- stage_id: `agent-implementer`",
        f"- attempt: {attempt}",
        "- role: `agent-implementer`",
    ]
    if model:
        lines.append(f"- model: `{model}`")
    lines += [
        "- check vs DONE.json: matched",
        "",
        "## DONE summary (from worker)",
        "",
        "done",
    ]
    return "\n".join(lines) + "\n"


# One attempt window per stage, plan → implementer → qa (17:05/17:15).
LOG_ONE_ATTEMPT = [
    "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
    "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
    "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
    "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
]


def _qa_active(proj: Path, todo: str) -> None:
    """State with qa-review running — implementer is a finished entry."""
    write_state(
        proj, stage_idx=2, stage_name="agent-qa-review",
        stage_kind="execute", todo_id=todo,
    )


class TestModelField:
    """Invariant 1(a)/(b): the entry's model from the stage facts."""

    def test_entry_carries_model_from_stage_facts(self, dash_project):
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), _stage_facts(model="vllm/llm"),
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")

        assert impl["model"] == "vllm/llm"
        # Invariant 4 (0176 regression): the attempt mapping and the
        # times are exactly the pre-chip behavior.
        assert impl["attempt"] == 1
        assert impl["started_epoch"] == _utc(2026, 10, 9, 17, 5)
        assert impl["ended_at"] != "—"
        assert impl["duration"] == "10m 0s"

    def test_entry_without_model_line_is_empty(self, dash_project):
        """Pre-M4.3 handoff (no Stage facts at all) → honest empty."""
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        old_body = (
            "# Handoff from `agent-implementer` (TODO TODO-0001)\n\n"
            "## Run facts\n\n- worker run: 1\n\ndone\n"
        )
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), old_body,
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")
        assert impl["model"] == ""

    def test_free_text_model_mention_does_not_leak(self, dash_project):
        """The model is the Stage facts line — a 'model:' mention in the
        worker's free text (DONE summary) must not reach the chip."""
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        body = (
            _stage_facts(model="vllm/llm")
            + "- model: `rogue/model` — a free-text mention\n"
        )
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), body,
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")
        assert impl["model"] == "vllm/llm"


    def test_done_summary_stage_facts_heading_does_not_leak(self, dash_project):
        """Scoping is by the section boundary, not just first match: a
        worker's DONE summary that quotes a '## Stage facts' heading with
        its own model line must not leak — the engine's section has no
        model here, so the chip is an honest «—». (The free-text test
        above cannot catch a scoping regression, because the engine's own
        model line is still the first match either way.)"""
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        body = (
            "# Handoff from `agent-implementer` (TODO TODO-0001)\n\n"
            "## Stage facts\n\n"
            "- stage_id: `agent-implementer`\n"
            "- attempt: 1\n"
            "- role: `agent-implementer`\n"
            "- check vs DONE.json: matched\n\n"
            "## DONE summary (from worker)\n\n"
            "Stage facts for reference:\n\n"
            "## Stage facts\n\n"
            "- model: `rogue/model`\n"
        )
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), body,
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")
        assert impl["model"] == ""


class TestTwoAttempts:
    """Invariant 1(c): the model stays in the attempt's own window."""

    def test_second_attempt_handoff_carries_second_attempt_model(self, dash_project):
        # Replan: verify sent implementer back (17:40 — attempt 2, open).
        # The handoff file was written at the END of attempt 2 (17:49):
        # its facts are attempt 2's.
        _write_log(dash_project, [
            "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
            "[2026-10-09T17:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:50:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
        ])
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 49), _stage_facts(model="vllm/second", attempt=2),
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if not c["active"])

        # The entry is attempt 2's — its times come from the SECOND
        # window and its model is the second attempt's record (not
        # borrowed from the first window's time span or vice versa).
        assert impl["attempt"] == 2
        assert impl["started_epoch"] == _utc(2026, 10, 9, 17, 40)
        assert impl["model"] == "vllm/second"
        # 0176 regression: the return mark is still there.
        assert "возврат:" in impl["direction"]

    def test_first_attempt_handoff_active_second_attempt(self, dash_project):
        """Replan in flight: the finished handoff is attempt 1's record;
        the active entry (attempt 2, current span) has no record yet."""
        _write_log(dash_project, [
            "[2026-10-09T17:00:00Z] Pipeline started with 4 stages: plan a b c",
            "[2026-10-09T17:00:00Z] Stage 0: plan (supervisor :: plan)",
            "[2026-10-09T17:05:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
            "[2026-10-09T17:15:00Z] Stage 2: agent-qa-review (agent-qa-review :: execute)",
            "[2026-10-09T17:25:00Z] Stage 3: verify (supervisor :: verify)",
            "[2026-10-09T17:40:00Z] Stage 1: agent-implementer (agent-implementer :: execute)",
        ])
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), _stage_facts(model="vllm/first", attempt=1),
        )
        write_state(
            dash_project, stage_idx=1, stage_name="agent-implementer",
            stage_kind="execute", todo_id="TODO-0001",
        )

        d = generate_state_dict(dash_project)
        chat = d["handoffs"]
        active = next(c for c in chat if c["active"])
        done = next(c for c in chat if not c["active"])

        assert done["attempt"] == 1
        assert done["started_epoch"] == _utc(2026, 10, 9, 17, 5)
        assert done["model"] == "vllm/first"
        # Invariant 2: the current span has no stage record — honest "".
        assert active["attempt"] == 2
        assert active["model"] == ""
        assert active["started_epoch"] == _utc(2026, 10, 9, 17, 40)


class TestCardRender:
    """Invariant 1(a) render: the card shows the chip; empty → «—»."""

    def test_card_renders_model_chip(self, dash_project):
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), _stage_facts(model="vllm/llm"),
        )
        _qa_active(dash_project, "TODO-0001")

        result = generate_dashboard(dash_project)
        html = result.read_text(encoding="utf-8")

        # The chip: existing pill style + the JS reads h.model.
        assert "chat-model" in html
        assert "h.model" in html
        # The state that feeds the chip carries the model.
        assert '"model": "vllm/llm"' in html
        # The empty branch renders «—».
        assert '"model": ""' in html

    def test_chip_dash_when_no_model(self, dash_project):
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14),
            "# Handoff from `agent-implementer` (TODO TODO-0001)\n\ndone\n",
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        impl = next(c for c in d["handoffs"] if c["role"] == "agent-implementer")
        assert impl["model"] == ""
        active = next(c for c in d["handoffs"] if c["active"])
        assert active["model"] == ""


class TestKeysUnbroken:
    """Invariant 3: only `model` was added — the rest of the contract
    is byte-identical in shape (0176/0175 consumers keep working)."""

    EXPECTED_KEYS = {
        "role", "icon", "color", "label", "content_html", "duration",
        "rev", "started_at", "ended_at", "started_epoch", "active",
        "awaiting", "is_verify", "line", "attempt", "direction", "model",
    }

    def test_completed_and_active_entries_carry_full_contract(self, dash_project):
        _write_log(dash_project, LOG_ONE_ATTEMPT)
        _write_handoff(
            dash_project, "agent-implementer", "TODO-0001",
            _utc(2026, 10, 9, 17, 14), _stage_facts(model="vllm/llm"),
        )
        _qa_active(dash_project, "TODO-0001")

        d = generate_state_dict(dash_project)
        assert d["handoffs"], "chat is empty — the fixture is broken"
        for entry in d["handoffs"]:
            assert set(entry) == self.EXPECTED_KEYS, (
                f"contract drift on {entry['role']}: "
                f"missing={self.EXPECTED_KEYS - set(entry)}, "
                f"extra={set(entry) - self.EXPECTED_KEYS}"
            )
