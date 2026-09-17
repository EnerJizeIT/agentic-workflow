"""NEG-2026-09 layer 2: exhaustive matrices for pure decision logic.

No hypothesis dependency — the space is small enough to enumerate.
These tests assert INVARIANTS, not exact outputs: wording/policies may
evolve, but the resolver must never crash, never invent actions, and never
strand the pipeline without a recovery path.
"""
from __future__ import annotations

import os

import pytest

from awf.pipeline import Stage
from awf.signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    read_signal_for_todo,
    signal_type,
)
from awf.transitions import resolve_transition

POLICIES = [
    "next",
    "stop",
    "escalate",
    "commit_and_next",
    "commit_and_report",
    "rollback_to:implement",
    "rollback_to:plan",
    "unknown_policy_value",
]
SIGNALS = [
    "done", "blocked", "approved", "rejected", "passed", "failed",
    "unknown", "", "totally-garbage",
]
VALID_ACTIONS = {
    "next", "rollback", "escalate", "stop", "commit_and_next", "commit_and_report",
}


class TestTransitionMatrix:
    @pytest.mark.parametrize("policy", POLICIES)
    def test_every_signal_yields_valid_action(self, policy):
        for sig in SIGNALS:
            stage = Stage(
                name="stage-x", role="worker", kind="execute",
                on_blocked=policy, on_approved=policy, on_rejected=policy,
                on_passed=policy, on_failed=policy,
            )
            action, target = resolve_transition(stage, sig)
            ctx = f"policy={policy!r} signal={sig!r}"
            assert action in VALID_ACTIONS, f"invalid action {action!r} ({ctx})"
            assert isinstance(target, str), ctx
            if action == "rollback":
                assert target, f"rollback without target ({ctx})"
            else:
                assert target == "", f"non-rollback carries target {target!r} ({ctx})"

    def test_blocked_stop_policy_stops(self):
        stage = Stage(name="s", role="worker", kind="execute", on_blocked="stop")
        assert resolve_transition(stage, "blocked") == ("stop", "")

    def test_unknown_signal_always_escalates(self):
        """P3 guarantee: garbage signal must keep a recovery path — never stop."""
        for policy in POLICIES:
            stage = Stage(
                name="s", role="worker", kind="execute",
                on_blocked=policy, on_approved=policy,
            )
            action, target = resolve_transition(stage, "totally-garbage")
            assert action == "escalate", f"policy={policy!r} → {action!r}"
            assert target == ""


class TestSignalTypeCorpus:
    CORPUS = [
        # canonical
        "DONE-TODO-0001", "BLOCKED-TODO-0001",
        "REVIEW-APPROVED-TODO-0001", "REVIEW-REJECTED-TODO-0001",
        "TEST-PASSED-TODO-0001", "TEST-FAILED-TODO-0001",
        # malformed / near-misses
        "DONE", "DONE-", "done-TODO-1", " BLOCKED-TODO-1", "BLOCKED-TODO-1 ",
        "REVIEW-", "REVIEW-APPROVED", "TEST-PASSED",
        "", "   ", "TODO-0001",
        "DONE\n-TODO-1", "DONE-TODO-1.ready", "DONE-TODO-😀", "ДОНЕ-TODO-1",
        "X" * 500,
    ]

    def test_classification_is_total_and_deterministic(self):
        valid = {
            "done", "blocked", "approved", "rejected", "passed", "failed", "unknown",
        }
        for name in self.CORPUS:
            first = signal_type(name)
            assert first in valid, f"{name!r} → {first!r}"
            assert signal_type(name) == first, f"non-deterministic for {name!r}"

    def test_prefix_shapes(self):
        for kind in ("plan", "execute", "verify", "nonsense", ""):
            prefixes = expected_signal_prefixes(kind)
            assert isinstance(prefixes, list)
            assert all(isinstance(p, str) and p for p in prefixes)
            assert len(prefixes) == len(set(prefixes)), f"dupes for kind={kind!r}"
        assert "DONE" in expected_signal_prefixes("execute")
        assert "BLOCKED" in expected_signal_prefixes("execute")


class TestSignalFileRaceRules:
    def _outbox(self, tmp_path):
        outbox = tmp_path / "outbox"
        outbox.mkdir(exist_ok=True)
        return outbox

    def test_latest_mtime_wins_among_multiple(self, tmp_path):
        """Worker's FINAL decision wins: DONE then BLOCKED → blocked."""
        outbox = self._outbox(tmp_path)
        done = outbox / "DONE-TODO-0001.ready"
        blocked = outbox / "BLOCKED-TODO-0001.ready"
        done.write_text("", encoding="utf-8")
        blocked.write_text("", encoding="utf-8")
        os.utime(done, (1_000_000, 1_000_000))
        os.utime(blocked, (2_000_000, 2_000_000))

        assert read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED") == "BLOCKED-TODO-0001"

        # Reverse the mtimes — DONE wins now.
        os.utime(done, (3_000_000, 3_000_000))
        assert read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED") == "DONE-TODO-0001"

    def test_signal_with_empty_companion_md_rejected(self, tmp_path):
        """P3: .ready + empty .md is NOT a signal (worker wrote nothing)."""
        outbox = self._outbox(tmp_path)
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
        (outbox / "DONE-TODO-0001.md").write_text("   \n", encoding="utf-8")
        assert read_signal_for_todo(outbox, "TODO-0001", "DONE") is None

        # Non-empty companion → accepted.
        (outbox / "DONE-TODO-0001.md").write_text("# done", encoding="utf-8")
        assert read_signal_for_todo(outbox, "TODO-0001", "DONE") == "DONE-TODO-0001"

    def test_legacy_short_and_typo_forms_accepted(self, tmp_path):
        outbox = self._outbox(tmp_path)
        (outbox / "DONE-0001.ready").write_text("", encoding="utf-8")
        assert read_signal_for_todo(outbox, "TODO-0001", "DONE") == "DONE-0001"

        (outbox / "DONE-0001.ready").unlink()
        (outbox / "DONE-TODO-0001.md.ready").write_text("", encoding="utf-8")
        assert read_signal_for_todo(outbox, "TODO-0001", "DONE") == "DONE-TODO-0001"

    def test_cleanup_only_touches_given_prefixes(self, tmp_path):
        """clean_stage_signals must not wipe other stages' signals."""
        outbox = self._outbox(tmp_path)
        (outbox / "DONE-TODO-0001.ready").write_text("", encoding="utf-8")
        (outbox / "DONE-TODO-0001.md").write_text("# done", encoding="utf-8")
        (outbox / "REVIEW-APPROVED-TODO-0001.ready").write_text("", encoding="utf-8")
        (outbox / "BRIEF-TODO-0002.ready").write_text("", encoding="utf-8")

        clean_stage_signals(outbox, "TODO-0001", "DONE")

        assert not (outbox / "DONE-TODO-0001.ready").exists()
        assert not (outbox / "DONE-TODO-0001.md").exists()
        # Other prefixes and other TODOs survive.
        assert (outbox / "REVIEW-APPROVED-TODO-0001.ready").exists()
        assert (outbox / "BRIEF-TODO-0002.ready").exists()

        # Idempotent: second call must not raise.
        clean_stage_signals(outbox, "TODO-0001", "DONE", "BLOCKED")


class TestRollbackTargetValidation:
    """NEG-2: rollback targets must exist in the pipeline.

    The old implicit default was ``rollback_to:implement`` — but generated
    pipelines name stages by role (``agent-implementer``), so every
    rejection hard-stopped with "Rollback target not found".
    """

    def test_default_policies_never_point_at_phantom_stage(self):
        stage = Stage(name="x", role="r")
        assert not stage.on_rejected.startswith("rollback_to:")
        assert not stage.on_failed.startswith("rollback_to:")

    def test_generated_worker_stages_have_safe_policies(self):
        from awf.api.setup import build_pipeline_stages

        stages = build_pipeline_stages(
            [{"agent": "agent-qa-review"}, {"agent": "agent-implementer"}]
        )
        names = {st["name"] for st in stages}
        for st in stages:
            for key in ("on_blocked", "on_rejected", "on_failed"):
                policy = st.get(key, "")
                if policy.startswith("rollback_to:"):
                    assert policy.split(":", 1)[1] in names, st

    def test_loader_warns_on_missing_rollback_target(self, tmp_path, capsys):
        from awf.pipeline import load_stages

        pipes = tmp_path / ".agentic" / "pipelines"
        pipes.mkdir(parents=True)
        (pipes / "bad.yaml").write_text(
            "stages:\n"
            '  - name: plan\n    role: supervisor\n'
            '  - name: worker\n    role: worker\n    on_rejected: "rollback_to:ghost"\n'
            '  - name: verify\n    role: supervisor\n',
            encoding="utf-8",
        )

        stages = load_stages(pipes / "bad.yaml")
        assert len(stages) == 3
        err = capsys.readouterr().err
        assert "'ghost'" in err and "rolls back" in err

    def test_loader_silent_for_valid_target(self, tmp_path, capsys):
        from awf.pipeline import load_stages

        pipes = tmp_path / ".agentic" / "pipelines"
        pipes.mkdir(parents=True)
        (pipes / "good.yaml").write_text(
            "stages:\n"
            '  - name: plan\n    role: supervisor\n'
            '  - name: implement\n    role: worker\n    on_rejected: "rollback_to:implement"\n'
            '  - name: verify\n    role: supervisor\n',
            encoding="utf-8",
        )

        load_stages(pipes / "good.yaml")
        assert capsys.readouterr().err == ""
