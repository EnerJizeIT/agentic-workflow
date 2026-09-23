"""FU-17b2 (TODO-0070): atomic rotation — two rotators don't lose an archive.

The old scheme did unlink + rename onto a single ``orchestrator.log.1``:
the second rotation (the next one, or a concurrent rotator) unlinked the
first archive — its lines were gone forever. The new scheme renames to a
unique ``<name>.<UTC-microsecond-stamp>-<pid>`` target and prunes only
the oldest archives beyond the keep limit.

- sequential rotations keep every archive (no lost lines)
- two concurrent rotators on one file: one wins, nothing is lost
- prune drops only the oldest archives beyond the keep limit
- the reader cold-passes ALL archives, oldest first
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path


def _logs(tmp_git_repo: Path) -> Path:
    logs = tmp_git_repo / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def _cfg(tmp_git_repo: Path, limit: int) -> None:
    (tmp_git_repo / ".agentic").mkdir(parents=True, exist_ok=True)
    (tmp_git_repo / ".agentic" / "config.yaml").write_text(
        f"automation:\n  log_max_bytes: {limit}\n", encoding="utf-8",
    )


def _write_big(logs: Path, prefix: str, n: int = 16) -> Path:
    log = logs / "orchestrator.log"
    lines = [f"[2026-09-19T05:00:00Z] {prefix} line {i}" for i in range(n)]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def _all_files(logs: Path) -> list[Path]:
    return [p for p in logs.iterdir() if p.name.startswith("orchestrator.log")]


def _archives(logs: Path) -> list[Path]:
    return [p for p in _all_files(logs) if p.name != "orchestrator.log"]


def _total_lines(logs: Path) -> int:
    n = 0
    for p in _all_files(logs):
        if p.is_file():
            n += len(p.read_text(encoding="utf-8").splitlines())
    return n


class TestSequentialRotations:
    """Two rotations in a row must keep two archives — no lost lines."""

    def test_two_rotations_no_lost_lines(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, _maybe_rotate

        _MAX_BYTES_CACHE.clear()
        _cfg(tmp_git_repo, 512)
        logs = _logs(tmp_git_repo)
        log = _write_big(logs, "round1")
        n = len(log.read_text(encoding="utf-8").splitlines())

        _maybe_rotate(log)
        assert not log.exists()  # rotated away

        _write_big(logs, "round2")  # re-creates the live file
        _maybe_rotate(log)

        archives = _archives(logs)
        assert len(archives) == 2, "two rotations must keep two archives"
        assert _total_lines(logs) == 2 * n, "no line may be lost"
        texts = " ".join(p.read_text(encoding="utf-8") for p in archives)
        assert "round1 line 0" in texts
        assert "round2 line 0" in texts

    def test_archive_name_is_unique_and_bounded(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, _maybe_rotate

        _MAX_BYTES_CACHE.clear()
        _cfg(tmp_git_repo, 512)
        logs = _logs(tmp_git_repo)
        log = _write_big(logs, "round1")
        _maybe_rotate(log)
        _write_big(logs, "round2")
        _maybe_rotate(log)

        names = sorted(p.name for p in _archives(logs))
        assert len(names) == 2
        for name in names:
            assert re.fullmatch(r"orchestrator\.log\.\d{8}T\d{12}Z-\d+", name), name
        assert names[0] != names[1]  # no single shared .1 target


class TestConcurrentRotators:
    """Two rotators racing on ONE live file: one wins, nothing is lost."""

    def test_two_threads_no_lost_lines(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, _maybe_rotate

        _MAX_BYTES_CACHE.clear()
        _cfg(tmp_git_repo, 512)
        logs = _logs(tmp_git_repo)
        log = _write_big(logs, "race", n=20)

        barrier = threading.Barrier(2)

        def spin() -> None:
            barrier.wait()
            _maybe_rotate(log)

        threads = [threading.Thread(target=spin) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert _total_lines(logs) == 20, "no line may be lost in the race"
        all_text = "".join(
            p.read_text(encoding="utf-8") for p in _all_files(logs) if p.is_file()
        )
        assert all_text.count("race line 0") == 1, "no line may be duplicated"


class TestSameStampNoOverwrite:
    """Two rotations landing on the same µs stamp (same pid) must not
    overwrite each other: rename onto an existing archive replaces it
    silently, losing the first archive's lines forever."""

    def test_frozen_clock_second_rotation_skips_not_overwrites(
        self, tmp_git_repo, monkeypatch,
    ):
        import datetime as _dt

        import awf._log as log_mod
        from awf._log import _MAX_BYTES_CACHE, _maybe_rotate

        _MAX_BYTES_CACHE.clear()
        _cfg(tmp_git_repo, 512)
        logs = _logs(tmp_git_repo)
        log = _write_big(logs, "round1")
        n = len(log.read_text(encoding="utf-8").splitlines())

        # Freeze the clock: every now() returns the same microsecond —
        # the worst case for name uniqueness, and the only way a
        # same-pid stamp collision can happen.
        frozen = _dt.datetime(2026, 9, 23, 17, 0, 0, 123456, tzinfo=_dt.timezone.utc)

        class _FrozenDatetime:
            @staticmethod
            def now(tz=None):
                return frozen

        monkeypatch.setattr(log_mod._dt, "datetime", _FrozenDatetime)

        _maybe_rotate(log)
        assert not log.exists()  # the first rotation took the file

        _write_big(logs, "round2")  # re-create the live file
        _maybe_rotate(log)  # same stamp again — must not replace archive 1

        all_text = "".join(
            p.read_text(encoding="utf-8") for p in _all_files(logs) if p.is_file()
        )
        assert "round1 line 0" in all_text  # the first archive survived
        assert "round2 line 0" in all_text
        assert _total_lines(logs) == 2 * n  # nothing lost, nothing duplicated


class TestPrune:
    """Prune drops only the oldest archives beyond the keep limit."""

    def test_prune_drops_oldest_keeps_newest(self, tmp_git_repo):
        from awf._log import (
            _MAX_BYTES_CACHE,
            DEFAULT_LOG_ARCHIVE_KEEP,
            _prune_archives,
        )

        _MAX_BYTES_CACHE.clear()
        logs = _logs(tmp_git_repo)
        log = logs / "orchestrator.log"
        log.write_text("live\n", encoding="utf-8")

        made: list[Path] = []
        for i in range(5):
            p = logs / f"orchestrator.log.20260919T05000{i}000000Z-1"
            p.write_text(f"arch {i}\n", encoding="utf-8")
            # explicit distinct mtimes — no reliance on FS clock granularity
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            made.append(p)
        # a foreign file in the same dir must survive pruning
        other = logs / "awf-agent-x-TODO-0001.out"
        other.write_text("worker\n", encoding="utf-8")

        _prune_archives(log)

        assert not made[0].exists()  # oldest dropped
        assert not made[1].exists()  # second-oldest dropped
        assert all(p.exists() for p in made[2:])  # the newest keep survive
        assert len(_archives(logs)) == DEFAULT_LOG_ARCHIVE_KEEP
        assert log.is_file()  # the live log is untouched
        assert other.is_file()  # foreign files are untouched

    def test_prune_below_limit_is_a_noop(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, _prune_archives

        _MAX_BYTES_CACHE.clear()
        logs = _logs(tmp_git_repo)
        log = logs / "orchestrator.log"
        log.write_text("live\n", encoding="utf-8")
        made = []
        for i in range(2):
            p = logs / f"orchestrator.log.20260919T05000{i}000000Z-1"
            p.write_text(f"arch {i}\n", encoding="utf-8")
            os.utime(p, (1_000_000 + i, 1_000_000 + i))
            made.append(p)

        _prune_archives(log)

        assert all(p.exists() for p in made)


class TestReaderReadsAllArchives:
    """The cold pass must feed every archive, oldest first, then current."""

    def test_cold_pass_feeds_archives_oldest_first(self, tmp_git_repo):
        from awf._log_reader import read_log_snapshot

        logs = _logs(tmp_git_repo)
        log = logs / "orchestrator.log"
        a1 = logs / "orchestrator.log.20260919T050000000000Z-1"
        a2 = logs / "orchestrator.log.20260919T060000000000Z-1"
        a1.write_text("[2026-09-19T05:00:00Z] Transition: old one\n", encoding="utf-8")
        a2.write_text("[2026-09-19T06:00:00Z] Transition: old two\n", encoding="utf-8")
        os.utime(a1, (1_000_000, 1_000_000))
        os.utime(a2, (1_000_001, 1_000_001))
        log.write_text(
            "[2026-09-19T07:00:00Z] Transition: new\n", encoding="utf-8",
        )

        snap = read_log_snapshot(log)

        assert [e["msg"] for e in snap.events] == [
            "→ Transition: old one",
            "→ Transition: old two",
            "→ Transition: new",
        ]
