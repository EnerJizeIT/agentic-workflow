"""FU-17b2: the shared incremental log reader (AUD15-01/AUD15-04/06/08).

- one parse of new bytes per poll, not four full reads (AUD15-01/AUD10-11)
- events identical to the legacy full-parse (_parse_log_events)
- orchestrator.log rotation via automation.log_max_bytes (AUD15-04)
- bounded tail reads for worker logs (AUD15-06)
- _suggest_timeout on the shared reader (AUD15-08)
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from awf._log_reader import read_log_snapshot, read_tail_lines

_TS = "%Y-%m-%dT%H:%M:%S"

# task id for the archive/tail fixtures (kept in one place)
_TASK_ID = "TODO-0001"


def _epoch(y, mo, d, h, mi, s) -> int:
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp())


def _count_reads(monkeypatch, name: str) -> list[int]:
    """Bytes read from the file with this name, via Path.open/read_bytes."""
    counts: list[int] = []
    real_open = Path.open
    real_read_bytes = Path.read_bytes

    def counting_open(self, *a, **kw):
        f = real_open(self, *a, **kw)
        real_read = f.read

        def read(size=-1):
            data = real_read(size)
            if self.name == name:
                counts.append(len(data))
            return data

        f.read = read
        return f

    def counting_read_bytes(self):
        data = real_read_bytes(self)
        if self.name == name:
            counts.append(len(data))
        return data

    monkeypatch.setattr(Path, "open", counting_open)
    monkeypatch.setattr(Path, "read_bytes", counting_read_bytes)
    return counts


def _write_log(proj: Path, lines: list[str]) -> Path:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = logs / "orchestrator.log"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


class TestIncrementalReader:
    """AUD15-01/AUD10-11: one parse of new bytes, not four full reads."""

    def test_snapshot_matches_expected_aggregates(self, tmp_git_repo):
        lines = [
            "[2026-09-18T10:00:00Z] Pipeline started with 5 stages: a",
            "[2026-09-18T10:00:01Z] Stage 1: old (old :: execute)",
            "[2026-09-18T10:30:00Z] Stage 4: verify (supervisor :: verify)",
            "[2026-09-18T10:31:00Z] Pipeline complete",
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:16:20Z] Stage 1: x (x :: execute)",
            "[2026-09-19T05:22:00Z] Stage 2: verify (supervisor :: verify)",
        ]
        log = _write_log(tmp_git_repo, lines)
        snap = read_log_snapshot(log)

        assert snap.closed_seconds == 1860  # run 1: 10:00:00 → 10:31:00
        # run 2 is still open — its start epoch is carried, not closed
        assert snap.open_run_start_epoch == _epoch(2026, 9, 19, 5, 16, 7)
        assert snap.run_start_epoch == _epoch(2026, 9, 19, 5, 16, 7)
        assert snap.first_agent_epoch == _epoch(2026, 9, 19, 5, 16, 20)
        assert snap.last_verify_epoch == _epoch(2026, 9, 19, 5, 22, 0)
        assert [n for _, n in snap.transitions] == ["plan", "x", "verify"]

    def test_events_match_legacy_full_parse(self, tmp_git_repo):
        """The incremental feed must yield EXACTLY the legacy parser's events."""
        from awf.api.dashboard import _parse_log_events

        lines = [
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:16:07Z] Stage 0: plan (supervisor :: plan)",
            "[2026-09-19T05:16:20Z] Stage 1: x (x :: execute)",
            "[2026-09-19T05:17:00Z] signal detected: DONE-1",
            "[2026-09-19T05:18:00Z] BD-36: checkpoint decision=approve",
            "[2026-09-19T05:18:01Z] BD-36: checkpoint opened waiting for user input",
            "[2026-09-19T05:18:02Z] BD-36: checkpoint still waiting for user",
            "[2026-09-19T05:19:00Z] Transition: stage=1 signal=DONE -> action=next",
            "[2026-09-19T05:20:00Z] Agent stage finished: rc=0",
            "[2026-09-19T05:20:01Z] Auto-committed 1 as abc1234",
            "[2026-09-19T05:21:00Z] salvage: worker died, supervisor invoked",
            "[2026-09-19T05:22:00Z] Stage 2: verify (supervisor :: verify)",
            "[2026-09-19T05:30:00Z] Pipeline complete",
            "not a log line at all",
        ]
        log = _write_log(tmp_git_repo, lines)
        snap = read_log_snapshot(log)
        assert snap.events == _parse_log_events("\n".join(lines))

    def test_second_poll_reads_only_new_bytes(self, tmp_git_repo, monkeypatch):
        counts = _count_reads(monkeypatch, "orchestrator.log")
        log = _write_log(tmp_git_repo, [
            "[2026-09-19T05:16:07Z] Pipeline started with 5 stages: plan x",
            "[2026-09-19T05:16:20Z] Stage 1: x (x :: execute)",
        ])

        snap1 = read_log_snapshot(log)
        cold = sum(counts)
        counts.clear()
        assert cold >= 60  # the cold pass reads the whole file

        appended = "[2026-09-19T05:22:00Z] Stage 2: verify (supervisor :: verify)\n"
        with log.open("a", encoding="utf-8") as f:
            f.write(appended)
        snap2 = read_log_snapshot(log)

        incr = sum(counts)
        # the 32 bytes beyond the append are the continuity fingerprint
        # (guards against rotated files that reused the inode)
        assert incr <= len(appended.encode()) + 32, (
            f"incremental poll read {incr} bytes — expected the appended "
            f"{len(appended)} bytes plus the 32 B fingerprint"
        )
        assert snap2.first_agent_epoch == snap1.first_agent_epoch
        assert snap2.last_verify_epoch == _epoch(2026, 9, 19, 5, 22, 0)

    def test_poll_on_10mb_log_is_fast_after_first(self, tmp_git_repo):
        """The audit's repro: ~10 MB log, warm poll must drop by an order of magnitude."""
        n = 130_000  # ~10.4 MB
        lines = [
            f"[2026-09-19T05:{(i // 60) % 60:02d}:{i % 60:02d}Z] "
            f"BD-30: stage x still waiting for signal (attempt {i})"
            for i in range(n)
        ]
        log = _write_log(tmp_git_repo, lines)
        assert log.stat().st_size > 9_000_000

        t0 = time.monotonic()
        read_log_snapshot(log)  # cold pass — pays the one-time parse
        cold_s = time.monotonic() - t0

        with log.open("a", encoding="utf-8") as f:
            f.write("[2026-09-19T06:00:00Z] Stage 2: verify (supervisor :: verify)\n")
        t0 = time.monotonic()
        snap = read_log_snapshot(log)
        incr_s = time.monotonic() - t0

        assert snap.last_verify_epoch == _epoch(2026, 9, 19, 6, 0, 0)
        # one appended line: the incremental poll must be ~100x cheaper
        # than the old 4-full-reads poll (250 ms/MB → >1 s on this log).
        assert incr_s < 0.2, f"incremental poll took {incr_s:.3f}s (cold: {cold_s:.3f}s)"

    def test_missing_log_returns_empty_snapshot(self, tmp_git_repo):
        log = tmp_git_repo / ".agentic" / "logs" / "orchestrator.log"
        snap = read_log_snapshot(log)
        assert snap.events == []
        assert snap.closed_seconds == 0
        assert snap.open_run_start_epoch is None

    def test_partial_line_completed_is_not_duplicated(self, tmp_git_repo):
        """A poll mid-write must not double-feed the held-back partial line.

        The offset points at the first unprocessed byte, so the incremental
        read already starts with the pending bytes — prepending
        self._pending on top of them duplicated the line (garbled event in
        the feed + offset drifting past EOF).
        """
        from awf._log_reader import OrchestratorLogReader

        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / "orchestrator.log"
        first = b"[2026-09-19T05:00:00Z] Pipeline started with 1 stages: x\n"
        second = b"[2026-09-19T05:00:01Z] Transition: a -> b\n"
        log.write_bytes(first + second[:-6])  # poll lands mid-write
        read_log_snapshot(log)

        log.write_bytes(first + second)  # writer finishes the line
        s = read_log_snapshot(log)

        assert [e["msg"] for e in s.events] == ["→ Transition: a -> b"]
        # the offset must track the file, not drift past EOF
        reader = OrchestratorLogReader.for_path(log)
        assert reader._offset == log.stat().st_size

    def test_trailing_partial_line_held_back(self, tmp_git_repo):
        """A half-written line must not be parsed until the writer finishes it."""
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log = logs / "orchestrator.log"
        log.write_bytes(b"[2026-09-19T05:00:00Z] Pipeline started with 1 stages: x\n")
        s1 = read_log_snapshot(log)
        assert s1.run_start_epoch is not None

        with log.open("ab") as f:
            f.write(b"[2026-09-19T05:00:01Z] Stage 0: x (x :: e")  # no \n yet
        s2 = read_log_snapshot(log)
        assert s2.transitions == [], "partial line must wait for its newline"

        with log.open("ab") as f:
            f.write(b"xecute)\n")
        s3 = read_log_snapshot(log)
        assert [n for _, n in s3.transitions] == ["x"]


class TestLogRotation:
    """AUD15-04: orchestrator.log is bounded by automation.log_max_bytes."""

    def _cfg(self, proj: Path, limit: int) -> None:
        (proj / ".agentic").mkdir(parents=True, exist_ok=True)
        (proj / ".agentic" / "config.yaml").write_text(
            f"automation:\n  log_max_bytes: {limit}\n", encoding="utf-8",
        )

    def test_rotates_past_limit(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, log

        _MAX_BYTES_CACHE.clear()
        self._cfg(tmp_git_repo, 512)
        logs = tmp_git_repo / ".agentic" / "logs"

        for i in range(20):
            log(logs, f"line {i} " + "y" * 90)

        live = logs / "orchestrator.log"
        archive = logs / "orchestrator.log.1"
        assert archive.is_file()
        assert live.stat().st_size < 1024  # bounded, not 20 lines
        content = live.read_text(encoding="utf-8")
        assert "line 19" in content
        assert "line 0" not in content  # rotated into the archive

    def test_default_limit_without_config(self, tmp_git_repo, monkeypatch):
        import awf._log as log_mod
        from awf._log import _MAX_BYTES_CACHE, log

        _MAX_BYTES_CACHE.clear()
        monkeypatch.setattr(log_mod, "DEFAULT_LOG_MAX_BYTES", 512)
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)

        for i in range(12):
            log(logs, f"line {i} " + "y" * 90)

        assert (logs / "orchestrator.log.1").is_file()
        assert logs.joinpath("orchestrator.log").stat().st_size < 1024

    def test_reader_survives_rotation(self, tmp_git_repo):
        from awf._log import _MAX_BYTES_CACHE, log

        _MAX_BYTES_CACHE.clear()
        self._cfg(tmp_git_repo, 512)
        logs = tmp_git_repo / ".agentic" / "logs"
        live = logs / "orchestrator.log"

        log(logs, "Pipeline complete")
        snap1 = read_log_snapshot(live)
        assert any(e["msg"] == "✅ Pipeline complete" for e in snap1.events)

        # write until exactly ONE rotation has happened (line length varies
        # by digit count — do not hard-code "how many lines rotate")
        archive = logs / "orchestrator.log.1"
        i = 0
        while not archive.is_file():
            log(logs, f"Transition: fill {i} (rotating)")
            i += 1
            assert i < 100, "rotation never triggered"
        assert "Pipeline complete" in archive.read_text(encoding="utf-8")

        snap2 = read_log_snapshot(live)

        # bounded size + one archive copy
        assert live.stat().st_size < 512 + 64
        assert archive.is_file()
        # pre-rotation history survived via the cold pass over the archive
        assert any(e["msg"] == "✅ Pipeline complete" for e in snap2.events)
        assert live.read_text(encoding="utf-8").count(f"fill {i - 1}") == 1

    def test_rotated_file_with_reused_inode_is_re_detected(self, tmp_git_repo):
        """Regression: overlayfs hands a new file the inode the renamed one had.

        Without the tail-fingerprint check the reader would treat the rotated
        (replaced) file as an append of the old one and keep serving stale
        aggregates from the wrong bytes.
        """
        from awf._log_reader import OrchestratorLogReader

        old = _write_log(tmp_git_repo, [
            "[2026-09-19T05:00:00Z] Pipeline complete",
            "[2026-09-19T05:00:01Z] Transition: old world",
        ])
        snap1 = read_log_snapshot(old)
        assert any("old world" in e["msg"] for e in snap1.events)

        # rotate: old file away (no .1 kept), new file GROWN past the old
        # offset — the shape that made the pre-fix reader take the append path
        prev_size = old.stat().st_size
        new_lines = [
            "[2026-09-19T06:00:00Z] Transition: new world one",
            "[2026-09-19T06:00:01Z] Transition: new world two",
            "[2026-09-19T06:00:02Z] Transition: new world three",
        ]
        old.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        assert old.stat().st_size > prev_size

        # simulate the FS lying: same inode number as the renamed file
        reader = OrchestratorLogReader.for_path(old)
        reader._inode = old.stat().st_ino

        snap2 = read_log_snapshot(old)

        # a cold pass (not an append-tail parse) must have happened: the
        # FIRST line of the new file is only reachable from offset 0
        assert any("new world one" in e["msg"] for e in snap2.events)
        assert not any("old world" in e["msg"] for e in snap2.events)


class TestTailRead:
    """AUD15-06: last-line cost independent of file size."""

    def test_worker_last_line_on_33mb_log(self, tmp_git_repo):
        from awf.api.dashboard import _read_worker_last_line

        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        big = logs / f"awf-agent-x-{_TASK_ID}.out"
        big.write_text(
            ("worker chatter line\n" * 1_900_000) + "final meaningful line\n",
            encoding="utf-8",
        )
        assert big.stat().st_size > 33_000_000

        assert _read_worker_last_line(tmp_git_repo, _TASK_ID) == "final meaningful line"

    def test_tail_read_bounded(self, tmp_git_repo, monkeypatch):
        counts = _count_reads(monkeypatch, "big.out")
        f = tmp_git_repo / "big.out"
        f.write_bytes(b"a" * 5_000_000 + b"\nfinal\n")

        lines = read_tail_lines(f)

        assert lines[-1] == "final"
        assert sum(counts) <= 65_536 + 1024, (
            f"read {sum(counts)} bytes from a 5 MB file — tail must be bounded"
        )

    def test_tail_read_drops_cut_first_line(self, tmp_path):
        f = tmp_path / "t.log"
        f.write_bytes(b"line\n" * 100_000)

        lines = read_tail_lines(f, max_bytes=100)

        assert lines and all(line == "line" for line in lines)

    def test_tail_read_missing_file(self, tmp_path):
        assert read_tail_lines(tmp_path / "nope.out") == []


class TestSuggestTimeoutSharedReader:
    """AUD15-08: _suggest_timeout rides the shared reader (no 5th full read)."""

    def test_incremental_after_first(self, tmp_git_repo, monkeypatch):
        from awf.api.wait_event import _suggest_timeout

        counts = _count_reads(monkeypatch, "orchestrator.log")
        base = datetime(2026, 9, 18, 8, 0, 0)
        lines = [
            f"[{(base + timedelta(minutes=12 * i)).strftime(_TS)}Z] "
            f"Stage {i}/4: stage{i} (role :: execute)"
            for i in range(4)
        ]
        log = _write_log(tmp_git_repo, lines)

        assert _suggest_timeout(tmp_git_repo) == 240
        counts.clear()

        with log.open("a", encoding="utf-8") as f:
            f.write(lines[-1] + "\n")  # one more identical-interval stamp
        assert _suggest_timeout(tmp_git_repo) == 240
        incr = sum(counts)
        assert incr <= 32 + 64, (
            f"second suggestion read {incr} bytes — expected the append "
            f"plus the 32 B continuity fingerprint"
        )
