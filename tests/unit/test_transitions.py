"""Unit tests for awf.transitions — resolve_transition for every (signal_type, policy) combo."""
import logging

from awf.pipeline import Stage
from awf.transitions import resolve_transition


class TestResolveTransition:

    def test_done_next(self) -> None:
        stage = Stage(name="v", role="s", on_approved="next")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("next", "")

    def test_done_commit_and_next(self) -> None:
        stage = Stage(name="v", role="s", on_approved="commit_and_next")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("commit_and_next", "")

    def test_done_commit_and_report(self) -> None:
        stage = Stage(name="v", role="s", on_approved="commit_and_report")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("commit_and_report", "")

    def test_approved_next(self) -> None:
        stage = Stage(name="r", role="rev", on_approved="next")
        action, target = resolve_transition(stage, "approved")
        assert (action, target) == ("next", "")

    def test_passed_next(self) -> None:
        stage = Stage(name="t", role="tester", on_passed="next")
        action, target = resolve_transition(stage, "passed")
        assert (action, target) == ("next", "")

    def test_passed_commit_and_next(self) -> None:
        stage = Stage(name="t", role="tester", on_passed="commit_and_next")
        action, target = resolve_transition(stage, "passed")
        assert (action, target) == ("commit_and_next", "")

    def test_rejected_rollback(self) -> None:
        stage = Stage(name="r", role="rev", on_rejected="rollback_to:implement")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("rollback", "implement")

    def test_rejected_replan(self) -> None:
        stage = Stage(name="v", role="s", on_rejected="replan")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("escalate", "")

    def test_failed_rollback(self) -> None:
        stage = Stage(name="t", role="tester", on_failed="rollback_to:implement")
        action, target = resolve_transition(stage, "failed")
        assert (action, target) == ("rollback", "implement")

    def test_failed_default_escalate(self) -> None:
        stage = Stage(name="t", role="tester", )
        action, target = resolve_transition(stage, "failed")
        assert (action, target) == ("rollback", "implement")

    def test_blocked_escalate(self) -> None:
        stage = Stage(name="i", role="w", on_blocked="escalate")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("escalate", "")

    def test_blocked_stop(self) -> None:
        stage = Stage(name="i", role="w", on_blocked="stop")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("stop", "")

    def test_blocked_rollback(self) -> None:
        stage = Stage(name="i", role="w", on_blocked="rollback_to:plan")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("rollback", "plan")

    def test_unknown_signal_escalates(self) -> None:
        """P3: unknown signal escalates to supervisor (recovery path, not dead-end stop)."""
        stage = Stage(name="x", role="r")
        action, target = resolve_transition(stage, "unknown")
        assert (action, target) == ("escalate", "")

    def test_unknown_signal_logs_warning(self, caplog) -> None:
        """Unknown signal type should emit a warning log for diagnostics."""
        with caplog.at_level(logging.WARNING):
            stage = Stage(name="impl", role="w", )
            resolve_transition(stage, "weird-signal")
        assert any("Unknown signal type" in r.message for r in caplog.records)
        assert any("'weird-signal'" in r.message for r in caplog.records)
        assert any("'impl'" in r.message for r in caplog.records)

    def test_rejected_rollback_to_custom_stage(self) -> None:
        stage = Stage(name="t", role="tester", on_rejected="rollback_to:plan")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("rollback", "plan")
