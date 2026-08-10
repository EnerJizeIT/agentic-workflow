"""Unit tests for awf.signals — signal_type, prefixes, read, clean, wait."""
import threading
from pathlib import Path

import pytest

from awf.signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    read_signal_for_todo,
    signal_type,
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
    """BD-29: prefixes are now kind-based (plan/execute/verify), not action-based."""

    def test_execute_returns_full_vocabulary(self) -> None:
        """Execute-kind stages accept all signal types — role decides what to emit."""
        result = expected_signal_prefixes("execute")
        assert "DONE" in result
        assert "BLOCKED" in result
        assert "REVIEW-APPROVED" in result
        assert "REVIEW-REJECTED" in result
        assert "TEST-PASSED" in result
        assert "TEST-FAILED" in result

    def test_plan_returns_empty(self) -> None:
        """Plan stages emit no worker signals (supervisor creates TODO, doesn't signal)."""
        result = expected_signal_prefixes("plan")
        assert result == []

    def test_verify_returns_empty(self) -> None:
        """Verify stages emit ACK (handled separately via _maybe_commit)."""
        result = expected_signal_prefixes("verify")
        assert result == []

    def test_unknown_kind_returns_empty(self) -> None:
        """Unknown kinds return empty list (no false matches)."""
        result = expected_signal_prefixes("salvage")
        assert result == []


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

    def test_latest_mtime_wins_over_prefix_order(self, tmp_path: Path) -> None:
        """KA2-5: if DONE and BLOCKED both exist, latest mtime wins."""
        import os
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        done = outbox / "DONE-TODO-0001.ready"
        blocked = outbox / "BLOCKED-TODO-0001.ready"
        done.write_text("")
        blocked.write_text("")
        # Force BLOCKED to have newer mtime than DONE
        os.utime(done, (1000, 1000))
        os.utime(blocked, (2000, 2000))
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result == "BLOCKED-TODO-0001"

    def test_done_wins_if_newer(self, tmp_path: Path) -> None:
        """KA2-5: if BLOCKED written first, then DONE — DONE wins."""
        import os
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        done = outbox / "DONE-TODO-0001.ready"
        blocked = outbox / "BLOCKED-TODO-0001.ready"
        done.write_text("")
        blocked.write_text("")
        # Force DONE to have newer mtime
        os.utime(blocked, (1000, 1000))
        os.utime(done, (2000, 2000))
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result == "DONE-TODO-0001"

    def test_legacy_short_blocked(self, tmp_path: Path) -> None:
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "BLOCKED-0003.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0003", "BLOCKED")
        assert result == "BLOCKED-0003"

    def test_ready_only_no_md_accepted(self, tmp_path: Path) -> None:
        """.ready without .md → signal IS accepted (md_file.exists() is False, so skip check)."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE")
        assert result == "DONE-TODO-0001"

    def test_ready_with_empty_md_rejected(self, tmp_path: Path) -> None:
        """.ready + empty .md → signal rejected (treat as not-ready)."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE")
        assert result is None

    def test_ready_with_content_md_accepted(self, tmp_path: Path) -> None:
        """.ready + non-empty .md → signal accepted."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("completed task\n")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE")
        assert result == "DONE-TODO-0001"

    def test_ready_with_whitespace_only_md_rejected(self, tmp_path: Path) -> None:
        """P3: .ready + .md with only whitespace → should be rejected (same as empty).

        Whitespace-only .md has st_size > 0 but no real content. This is a
        signal quality issue — worker created a placeholder .md without content.
        """
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("   \n  \n")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE")
        assert result is None  # whitespace-only .md = no real signal

    def test_empty_md_falls_through_to_next_prefix(self, tmp_path: Path) -> None:
        """When DONE has empty .md but BLOCKED has .ready, BLOCKED should be found."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("")
        (outbox / "BLOCKED-TODO-0001.ready").write_text("")
        result = read_signal_for_todo(outbox, "TODO-0001", "DONE", "BLOCKED")
        assert result == "BLOCKED-TODO-0001"


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

    def test_wait_ignores_ready_with_empty_md(self, tmp_path: Path) -> None:
        """wait_for_signal should NOT return when .ready exists but .md is empty."""
        outbox = tmp_path / "outbox"
        outbox.mkdir()
        (outbox / "DONE-TODO-0001.ready").write_text("")
        (outbox / "DONE-TODO-0001.md").write_text("")
        with pytest.raises(TimeoutError):
            wait_for_signal(outbox, "TODO-0001", "DONE", timeout=0.3, interval=0.1)
