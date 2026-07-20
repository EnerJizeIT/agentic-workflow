"""Unit tests for awf.transitions — resolve_transition for every (signal_type, policy) combo."""
from awf.pipeline import Stage
from awf.transitions import resolve_transition


class TestResolveTransition:

    def test_done_next(self) -> None:
        stage = Stage(name="v", role="s", action="verify", on_approved="next")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("next", "")

    def test_done_commit_and_next(self) -> None:
        stage = Stage(name="v", role="s", action="verify", on_approved="commit_and_next")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("commit_and_next", "")

    def test_done_commit_and_report(self) -> None:
        stage = Stage(name="v", role="s", action="verify", on_approved="commit_and_report")
        action, target = resolve_transition(stage, "done")
        assert (action, target) == ("commit_and_report", "")

    def test_approved_next(self) -> None:
        stage = Stage(name="r", role="rev", action="review_code", on_approved="next")
        action, target = resolve_transition(stage, "approved")
        assert (action, target) == ("next", "")

    def test_passed_next(self) -> None:
        stage = Stage(name="t", role="tester", action="run_tests", on_passed="next")
        action, target = resolve_transition(stage, "passed")
        assert (action, target) == ("next", "")

    def test_passed_commit_and_next(self) -> None:
        stage = Stage(name="t", role="tester", action="run_tests", on_passed="commit_and_next")
        action, target = resolve_transition(stage, "passed")
        assert (action, target) == ("commit_and_next", "")

    def test_rejected_rollback(self) -> None:
        stage = Stage(name="r", role="rev", action="review_code", on_rejected="rollback_to:implement")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("rollback", "implement")

    def test_rejected_replan(self) -> None:
        stage = Stage(name="v", role="s", action="verify", on_rejected="replan")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("escalate", "")

    def test_failed_rollback(self) -> None:
        stage = Stage(name="t", role="tester", action="run_tests", on_failed="rollback_to:implement")
        action, target = resolve_transition(stage, "failed")
        assert (action, target) == ("rollback", "implement")

    def test_failed_default_escalate(self) -> None:
        stage = Stage(name="t", role="tester", action="run_tests")
        action, target = resolve_transition(stage, "failed")
        assert (action, target) == ("rollback", "implement")

    def test_blocked_escalate(self) -> None:
        stage = Stage(name="i", role="w", action="execute_todo", on_blocked="escalate")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("escalate", "")

    def test_blocked_stop(self) -> None:
        stage = Stage(name="i", role="w", action="execute_todo", on_blocked="stop")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("stop", "")

    def test_blocked_rollback(self) -> None:
        stage = Stage(name="i", role="w", action="execute_todo", on_blocked="rollback_to:plan")
        action, target = resolve_transition(stage, "blocked")
        assert (action, target) == ("rollback", "plan")

    def test_unknown_signal_stops(self) -> None:
        stage = Stage(name="x", role="r", action="a")
        action, target = resolve_transition(stage, "unknown")
        assert (action, target) == ("stop", "")

    def test_rejected_rollback_to_custom_stage(self) -> None:
        stage = Stage(name="t", role="tester", action="run_tests", on_rejected="rollback_to:plan")
        action, target = resolve_transition(stage, "rejected")
        assert (action, target) == ("rollback", "plan")
