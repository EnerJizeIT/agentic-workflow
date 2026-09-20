"""Shared incremental reader for orchestrator.log (AUD15-01, AUD15-08).

Before this module every /api/state poll read the WHOLE log four times
(dashboard) and a fifth in wait_for_event — O(size) parse per poll,
~250 ms/MB. This module keeps a per-path offset cache: only the bytes
appended since the previous snapshot are parsed, and the aggregates
(events, run spans, stage stamps, total elapsed) accumulate.

Rotation (AUD15-04, orchestrator.log.1) is handled by inode tracking:
a new inode triggers a one-time cold pass over archive + current file,
so multi-run history survives rotation.

The writer (awf/_log.py) opens the file per line with O_APPEND — the
file is append-only between rotations, which is what makes offset
tracking sound.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Event lines: [2026-08-05T15:42:19Z] message  (Z optional, as in the
# original _parse_log_events — the fallback branch relies on it).
_EVENT_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})Z?)\]\s*(.*)")
# Strict timestamp for the run/stage machines (original parsers required Z).
_STRICT_TS = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]")
# Run scope: the ts must sit right before 'Pipeline started'
# (original _scoped_log_text marker).
_RUN_START = re.compile(
    r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\]\s*Pipeline started"
)
_STAGE = re.compile(r"Stage\s+\d+:\s+(\S+)")
_STAGE_MSG = re.compile(r"Stage\s+\d+:\s+(\S+)")
_AGENT_STAGE = re.compile(r"Stage\s+\d+:\s+\S+\s+\([^)]*::\s*execute")
_VERIFY_STAGE = re.compile(r"Stage\s+\d+:\s+\S+\s+\([^)]*::\s*verify")
# SPEC A-run stage stamps (original _suggest_timeout pattern, N/M form).
_STAGE_STAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\] Stage \d+/\d+:")

_RUN_STARTED = "Pipeline started"
_RUN_COMPLETE = "Pipeline complete"

EVENTS_CAP = 400  # the dashboard shows the last 30
STAGE_STAMP_CAP = 32  # the suggestion uses the last 5 deltas

_TS_FMT = "%Y-%m-%dT%H:%M:%S"


def _parse_dt(raw: str) -> datetime | None:
    try:
        return datetime.strptime(raw, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _epoch(dt: datetime | None) -> int | None:
    return int(dt.timestamp()) if dt is not None else None


def _classify_event_line(line: str) -> dict | None:
    """AUD15-01: one line of the original _parse_log_events logic.

    Kept line-for-line equivalent to awf.api.dashboard._parse_log_events
    so the incremental feed yields the same events the full parse did.
    """
    m = _EVENT_TS.match(line)
    if not m:
        return None
    try:
        utc_dt = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone()
        ts_short = local_dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        ts_short = f"{m.group(2)}:{m.group(3)}:{m.group(4)}"
    msg = m.group(5).strip()

    stage_m = _STAGE_MSG.match(msg)
    if stage_m:
        return {"ts": ts_short, "msg": f"▶ {stage_m.group(1)}", "type": "start"}

    if "signal detected" in msg.lower():
        return {"ts": ts_short, "msg": f"📨 {msg[:80]}", "type": "info"}

    if "BD-36" in msg and "checkpoint" in msg.lower():
        if "decision=approve" in msg or "auto-approve" in msg:
            return {"ts": ts_short, "msg": "⏸ Checkpoint auto-approved", "type": "info"}
        if "still waiting" in msg.lower():
            return None
        if "decision" in msg:
            return {"ts": ts_short, "msg": "⏸ Checkpoint decision recorded", "type": "info"}
        return {"ts": ts_short, "msg": "⏸ Checkpoint opened — needs user", "type": "warn"}

    if "Pipeline complete" in msg:
        return {"ts": ts_short, "msg": "✅ Pipeline complete", "type": "done"}

    if "salvage" in msg.lower() and "waiting" not in msg.lower():
        return {"ts": ts_short, "msg": f"🔧 {msg[:80]}", "type": "warn"}

    if msg.startswith("Transition:"):
        return {"ts": ts_short, "msg": f"→ {msg[:80]}", "type": "info"}

    if "Agent stage finished" in msg:
        return {"ts": ts_short, "msg": "✓ Stage complete", "type": "done"}

    if "Auto-committed" in msg or "auto-committed" in msg.lower():
        return {"ts": ts_short, "msg": "📦 Committed", "type": "done"}

    return None


class _Aggregates:
    """Mutable per-line accumulator (one orchestrator.log)."""

    __slots__ = (
        "events", "run_start", "first_agent", "last_verify", "transitions",
        "closed_seconds", "open_start", "stage_stamps",
    )

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.run_start: datetime | None = None      # last 'Pipeline started' (strict)
        self.first_agent: datetime | None = None    # first agent stage of current run
        self.last_verify: datetime | None = None    # last verify line of current run
        self.transitions: list[tuple[int, str]] = []  # current run (epoch, stage name)
        self.closed_seconds: int = 0                # across ALL runs (substring rule)
        self.open_start: datetime | None = None     # open run of the total machine
        self.stage_stamps: list[int] = []           # rolling 'Stage N/M:' epochs

    def feed_line(self, line: str) -> None:
        event = _classify_event_line(line)
        if event is not None:
            self.events.append(event)
            if len(self.events) > EVENTS_CAP:
                del self.events[: len(self.events) - EVENTS_CAP]

        # The strict-timestamp machines only fire on stage/run lines —
        # 99% of a live log (BD-30 waiting noise, worker chatter) skips
        # the strptime entirely.
        is_run_line = _RUN_STARTED in line or _RUN_COMPLETE in line
        if "Stage" not in line and not is_run_line:
            return

        # Run scope (strict marker, as _scoped_log_text): the original sliced
        # the text at the marker even when its timestamp did not parse —
        # so the scope resets either way, and the start time is unknown.
        m_start = _RUN_START.search(line)
        if m_start is not None:
            self.run_start = _parse_dt(m_start.group(1))
            self.first_agent = None
            self.last_verify = None
            self.transitions = []

        # Total-across-runs machine (substring rule, as _total_elapsed)
        if is_run_line:
            tm = _STRICT_TS.search(line)
            dt2 = _parse_dt(tm.group(1)) if tm else None
            if dt2 is not None:
                if _RUN_STARTED in line:
                    if self.open_start is not None:
                        self.closed_seconds += int((dt2 - self.open_start).total_seconds())
                    self.open_start = dt2
                elif self.open_start is not None:
                    self.closed_seconds += int((dt2 - self.open_start).total_seconds())
                    self.open_start = None

        # Current-run stage data (strict ts, as _run_elapsed/_stage_spans)
        tm = _STRICT_TS.search(line)
        dt3 = _parse_dt(tm.group(1)) if tm else None
        if dt3 is not None:
            if _AGENT_STAGE.search(line) and self.first_agent is None:
                self.first_agent = dt3
            if _VERIFY_STAGE.search(line):
                self.last_verify = dt3
            sm = _STAGE.search(line)
            if sm:
                self.transitions.append((int(dt3.timestamp()), sm.group(1)))

        sm2 = _STAGE_STAMP.search(line)
        if sm2 is not None:
            dt4 = _parse_dt(sm2.group(1))
            if dt4 is not None:
                self.stage_stamps.append(int(dt4.timestamp()))
                if len(self.stage_stamps) > STAGE_STAMP_CAP:
                    del self.stage_stamps[: len(self.stage_stamps) - STAGE_STAMP_CAP]


@dataclass
class LogSnapshot:
    """Immutable view of the log aggregates for one poll."""

    events: list[dict] = field(default_factory=list)
    run_start_epoch: int | None = None
    first_agent_epoch: int | None = None
    last_verify_epoch: int | None = None
    transitions: list[tuple[int, str]] = field(default_factory=list)
    closed_seconds: int = 0
    open_run_start_epoch: int | None = None
    stage_stamps: list[int] = field(default_factory=list)

    @classmethod
    def empty(cls) -> LogSnapshot:
        return cls()


class OrchestratorLogReader:
    """Offset-cached incremental reader, one instance per log path."""

    _readers: dict[str, OrchestratorLogReader] = {}
    _readers_lock = threading.Lock()
    _MAX_CACHED = 64

    _TAIL_BYTES = 32  # continuity fingerprint size

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._inode: int | None = None
        self._offset = 0
        self._pending: bytes = b""
        self._tail: bytes = b""  # last bytes consumed — detects file replacement
        self._agg = _Aggregates()

    @classmethod
    def for_path(cls, log_file: Path) -> OrchestratorLogReader:
        key = str(log_file)
        with cls._readers_lock:
            reader = cls._readers.get(key)
            if reader is None:
                if len(cls._readers) >= cls._MAX_CACHED:
                    cls._readers.clear()
                reader = cls()
                cls._readers[key] = reader
            return reader

    def snapshot(self, log_file: Path) -> LogSnapshot:
        """Aggregates for the log; parses only bytes appended since last call."""
        with self._lock:
            if not log_file.is_file():
                return LogSnapshot.empty()
            try:
                st = log_file.stat()
            except OSError:
                return LogSnapshot.empty()
            size = st.st_size
            if st.st_ino != self._inode or size < self._offset:
                self._cold_pass(log_file, st)
            elif size > self._offset:
                # Same inode + grown: normally a plain append. But some
                # filesystems (overlayfs) hand a NEW file the same inode
                # number a just-renamed one had — a rotated log would then
                # be misread as an append of the old one. The tail
                # fingerprint distinguishes the two.
                if self._tail and not self._check_continuity(log_file):
                    self._cold_pass(log_file, st)
                else:
                    self._incremental(log_file)
            return self._to_snapshot()

    def _check_continuity(self, log_file: Path) -> bool:
        """The bytes right before the offset must still be what we consumed."""
        try:
            with log_file.open("rb") as f:
                f.seek(self._offset - len(self._tail))
                return f.read(len(self._tail)) == self._tail
        except OSError:
            return True  # unreadable — let the incremental pass surface it

    def _cold_pass(self, log_file: Path, st) -> None:
        self._agg = _Aggregates()
        self._pending = b""
        self._tail = b""
        archive = log_file.parent / (log_file.name + ".1")
        if archive.is_file():
            try:
                self._feed(archive.read_bytes())
            except OSError:
                pass
        try:
            data = log_file.read_bytes()
        except OSError:
            data = b""
        self._feed(data)
        # Offset/tail count bytes of the CURRENT file; a trailing partial
        # line stays pending until the writer finishes it. len(data), not
        # st.st_size — the file may have grown between stat() and read().
        consumed = len(data) - len(self._pending)
        self._tail = data[max(0, consumed - self._TAIL_BYTES):consumed]
        self._offset = consumed
        self._inode = st.st_ino

    def _incremental(self, log_file: Path) -> None:
        try:
            with log_file.open("rb") as f:
                f.seek(self._offset)
                data = f.read()
        except OSError:
            return
        # The offset points at the first UNPROCESSED byte, so data already
        # starts with the held-back partial line — do not prepend
        # self._pending again (that would feed the line twice and drift the
        # offset past EOF).
        self._pending = b""
        self._feed(data)
        consumed = len(data) - len(self._pending)
        if consumed > 0:
            self._tail = (
                self._tail + data[max(0, consumed - self._TAIL_BYTES):consumed]
            )[-self._TAIL_BYTES:]
        self._offset += consumed

    def _feed(self, data: bytes) -> None:
        buf = self._pending + data
        if buf and not buf.endswith(b"\n"):
            cut = buf.rfind(b"\n")
            if cut >= 0:
                complete, self._pending = buf[: cut + 1], buf[cut + 1:]
            else:
                complete, self._pending = b"", buf
        else:
            complete, self._pending = buf, b""
        if not complete:
            return
        text = complete.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if line.endswith("\r"):
                line = line[:-1]
            if line:
                self._agg.feed_line(line)

    def _to_snapshot(self) -> LogSnapshot:
        a = self._agg
        return LogSnapshot(
            events=list(a.events),
            run_start_epoch=_epoch(a.run_start),
            first_agent_epoch=_epoch(a.first_agent),
            last_verify_epoch=_epoch(a.last_verify),
            transitions=list(a.transitions),
            closed_seconds=a.closed_seconds,
            open_run_start_epoch=_epoch(a.open_start),
            stage_stamps=list(a.stage_stamps),
        )


def read_log_snapshot(log_file: Path) -> LogSnapshot:
    """Process-wide cached snapshot for an orchestrator.log path."""
    return OrchestratorLogReader.for_path(log_file).snapshot(log_file)


# ─── AUD15-06: bounded tail reads (worker logs, TEST-RESULTS) ──────────

def read_tail_lines(
    path: Path, max_bytes: int = 64 * 1024, max_lines: int = 200,
) -> list[str]:
    """Last lines of a file without reading the whole file.

    Seeks ``max_bytes`` back from EOF. When the window cut a line in half
    the partial first line is dropped. Cost is O(max_bytes), not O(size).
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]
    return lines[-max_lines:]


__all__ = [
    "LogSnapshot",
    "OrchestratorLogReader",
    "read_log_snapshot",
    "read_tail_lines",
]
