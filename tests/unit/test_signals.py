"""Unit tests for awf.signals — signal_type, prefixes, read, clean, wait."""
import threading
from pathlib import Path
import pytest

from awf.signals import (
    signal_type,
    expected_signal_prefixes,
    read_signal_for_todo,
    clean_stage_signals,
    wait_for_signal,
)


class TestSignalType:

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("DONE-TODO-0001", "done"),
            ("DONE-0001", "done"),
            ("BLOCKED-TODO-0001", "blocked"),
            ("BLOCKED-0001", "blocked"),
            ("REVIEW-APPROVED-TODO-0001", "approved"),
            ("REVIEW-REJECTED-TODO-0001", "rejected"),
            ("TEST-PASSED-TODO-0001", "passed"),
            ("TEST-FAILED-TODO-0001", "failed"),
            ("WEIRD-TODO-0001", "unknown"),
            ("RANDOM", "unknown"),
        ],
    )
    def test_signal_type_classification(self, filename: str, expected: str) -> None:
        assert signal_type(filename) == expected


class TestExpectedSignalPrefixes:

    def test_execute_todo(self) -> None:
        result = expected_signal_prefixes("execute_todo")
        assert "DONE" in result
        assert "BLOCKED" in result

    def test_review_code(self) -> None:
        result = expected_signal_prefixes("review_code")
        assert "REVIEW-APPROVED" in result
        assert "REVIEW-REJECTED" in result
        assert "BLOCKED" in result

    def test_run_tests(self) -> None:
        result = expected_signal_prefixes("run_tests")
        assert "TEST-PASSED" in result
        assert "TEST-FAILED" in result
        assert "BLOCKED" in result

    def test_unknown_action_returns_all(self) -> None:
        result = expected_signal_prefixes("unknown_action")
        assert "DONE" in result
        assert "BLOCKED" in result
        assert "REVIEW-APPROVED" in result
        assert "REVIEW-REJECTED" in result
        assert "TEST-PASSED" in result
        assert "TEST-FAILED" in result

    def test_audit_code(self) -> None:
        result = expected_signal_prefixes("audit_code")
        assert "REVIEW-APPROVED" in result
        assert "REVIEW-REJECTED" in result
        assert "BLOCKED" in result


class TestReadSignalForTodo:

    def test_canonical_form(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result == "DONE-TODO-0001"

    def test_legacy_short_form(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-0002.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0002", "DONE", "BLOCKED")
        assert result == "DONE-0002"

    def test_prefix_filtering_ignores_stale_done(self, tmp_path: Path) -> None:
        """Stale DONE must NOT be picked up when polling for REVIEW-* signals."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("")
        result = read_signal_for_todo(
            outbox, "TODO-0001", "REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED"
        )
        assert result is None

    def test_review_signal_found(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "REVIEW-APPROVED-TODO-0001.ready").write_text("")
        result = read_signal_for_todo(
            outbox, "TODO-0001", "REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED"
        )
        assert result == "REVIEW-APPROVED-TODO-0001"

    def test_no_signal(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result is None

    def test_blocked_signal_canonical(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "BLOCKED-TODO-0001.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result == "BLOCKED-TODO-0001"

    def test_legacy_short_blocked(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "BLOCKED-0003.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0003", "BLOCKED")
        assert result == "BLOCKED-0003"


class TestCleanStageSignals:

    def test_removes_own_signals_preserves_earlier(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("")
        (outbox / "REVIEW-APPROVED-TODO-0001.ready").write_text("")
        (outbox / "REVIEW-APPROVED-TODO-0001.md").write_text("")

        clean_stage_signals(outbox, "TODO-0001", "REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED")

        assert not (outbox / "REVIEW-APPROVED-TODO-0001.ready").exists()
        assert not (outbox / "REVIEW-APPROVED-TODO-0001.md").exists()
        assert (outbox / "DONE-TODO-0001.ready").exists()
        assert (outbox / "DONE-TODO-0001.md").exists()

    def test_removes_legacy_short_form(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "REVIEW-REJECTED-0001.ready").write_text("")

        clean_stage_signals(outbox, "TODO-0001", "REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED")

        assert not (outbox / "REVIEW-REJECTED-0001.ready").exists()

    def test_clean_noop_when_missing(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        clean_stage_signals(outbox, "TODO-9999", "DONE", "BLOCKED")


class TestWaitForSignal:

    def test_returns_signal_when_present(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        result = wait_for_signal(outbox, "TODO-0001", "DONE", "BLOCKED", timeout=1, interval=0.1)
        assert result == "DONE-TODO-0001"

    def test_timeout_error(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        with pytest.raises(TimeoutError):
            wait_for_signal(outbox, "TODO-0001", "DONE", "BLOCKED", timeout=1, interval=0.1)

    def test_signal_appears_mid_wait(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()

        def create_signal_later():
            import time
            time.sleep(0.15)
            (outbox / "DONE-TODO-0001.ready").write_text("")

        t = threading.Thread(target=create_signal_later, daemon=True)
        t.start()
        result = wait_for_signal(outbox, "TODO-0001", "DONE", "BLOCKED", timeout=3, interval=0.05)
        assert result == "DONE-TODO-0001"
        t.join(timeout=2)

    def test_default_prefixes(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "BLOCKED-TODO-0001.ready").write_text("")
        result = wait_for_signal(outbox, "TODO-0001", timeout=1, interval=0.1)
        assert result == "BLOCKED-TODO-0001"
