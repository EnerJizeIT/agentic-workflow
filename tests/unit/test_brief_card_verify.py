"""TODO-0348 (IMP5): verify facts of the current pass (AC26–AC29, AC43).

The card reads the same IMP3 readers by NAME; an old artifact is hidden, a
missing one is a fact, and the commit-gate refusal carries its reason.
"""
from __future__ import annotations

from datetime import datetime, timezone

from awf.brief import build_brief

from .brief_card_support import (
    EMERGENCY_PACK,
    FAILED_PACK,
    FIRST_FAILURE,
    OK_PACK,
    TODO,
    project,
    verify_status,
    write_gates,
    write_state,
)


class TestVerifyFacts:
    def test_failed_pack_and_first_reason(self, tmp_path):
        root = project(tmp_path)
        write_state(root)
        write_gates(root, FAILED_PACK)
        r = build_brief(root, status=verify_status())
        assert r.situation == "S8"
        assert r.verify["unit"] == TODO
        assert r.verify["status"] == "failed"
        assert r.verify["failed_sections"] == ["contracts"]
        assert r.verify["first_failure"] == FIRST_FAILURE
        assert r.verify["waiting_on"] == "supervisor"
        assert r.verify["close_with"] == "awf_approve"
        assert "contracts" in r.text

    def test_ok_pack_waits_for_the_commit_gate(self, tmp_path):
        root = project(tmp_path)
        write_state(root)
        write_gates(root, OK_PACK)
        r = build_brief(root, status=verify_status())
        assert r.verify["status"] == "ok"
        assert r.verify["waiting_on"] == "commit"
        assert r.verify["close_with"] == ""

    def test_missing_pack_points_at_verify_pack(self, tmp_path):
        root = project(tmp_path)
        write_state(root)
        r = build_brief(root, status=verify_status())
        assert r.verify["status"] == "missing"
        assert r.verify["close_with"] == "awf_verify_pack"
        assert "no pack yet" in r.text

    def test_old_artifact_is_hidden_outside_verify(self, tmp_path):
        """QA A39: the state clock must come from ``now``, not a fixed date.

        The artifact is written with an mtime of ``now - 24h``; a hardcoded
        state timestamp is "fresh" as soon as the calendar passes it (this
        test went red at 2026-11-03 00:00 UTC with no code change). The two
        sides now share one clock, which is the fact the reader compares.
        """
        root = project(tmp_path)
        now = datetime.now(timezone.utc).isoformat()
        write_state(root, updated=now)
        write_gates(root, FAILED_PACK, old=True)
        state = verify_status(
            expected_action="plan stage — wait",
            current_stage_name="plan",
            current_stage_kind="plan",
            state_updated_at=now,
        )
        r = build_brief(root, status=state)
        assert r.verify == {}

    def test_no_current_todo_means_no_verdict(self, tmp_path):
        root = project(tmp_path)
        write_gates(root, FAILED_PACK)
        r = build_brief(root, status=verify_status(current_todo=None))
        assert r.verify == {}

    def test_commit_refusal_reason(self, tmp_path):
        root = project(tmp_path)
        write_state(root)
        write_gates(root, FAILED_PACK)
        (root / ".agentic" / "context" / f"COMMIT-AUDIT-{TODO}.json").write_text(
            '{"todo_id": "TODO-0326", "status": "refused", "reason": "sha mismatch"}',
            encoding="utf-8",
        )
        r = build_brief(root, status=verify_status())
        assert r.verify["commit"] == {"status": "refused", "reason": "sha mismatch"}
        assert "sha mismatch" in r.text

    def test_no_commit_artifact(self, tmp_path):
        root = project(tmp_path)
        write_state(root)
        write_gates(root, FAILED_PACK)
        r = build_brief(root, status=verify_status())
        assert r.verify["commit"] == {"status": "missing", "reason": ""}
        assert "no attempt yet" in r.text

    def test_emergency_note_is_an_error(self, tmp_path):
        """AC29/T8: the crash note is the reason, not just the verdict.

        QA: the split kept only the verdict line and dropped the reason
        assertion from the pre-split test — a card that hides ``note`` would
        pass again.
        """
        root = project(tmp_path)
        write_state(root)
        write_gates(root, EMERGENCY_PACK)
        r = build_brief(root, status=verify_status())
        assert r.verify["status"] == "error"
        assert r.verify["note"] == "verify-pack failed before measuring anything:"
        assert "did not run" in r.text
        assert "verify-pack failed before measuring anything:" in r.text
