"""Autonomous run (забег) state — SPEC A-run v1.

A run is a queue of TODO ids with gates. The supervisor chat drives the
loop; awf keeps the state and enforces the mechanical stop-list:

- queue exhausted or budget exceeded
- stop flags on the next TODO (phase boundary, external audit, owner decision)
- the next TODO was rejected twice already
- the previous TODO is not finished (not archived)

Stored in ``.agentic/state/run.yaml`` — a SEPARATE file from current.yaml so
stage-state writes never clobber the run (same reason as dashboard_port).
Crash-safe: the file survives process death; the supervisor re-reads it via
``awf_run_status`` and continues with ``awf_run_next``.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import paths
from ._atomic import atomic_write_text
from ._log import log as _log

# Same id format the run API validates (awf/api/run.py::_TODO_RE).
_TODO_ID_RE = re.compile(r"^TODO-\d{4,}$")


def run_file(project_dir: Path) -> Path:
    """Path of the run state file."""
    return paths.agentic_dir(project_dir) / "state" / "run.yaml"


def _run_shape_ok(state: dict) -> bool:
    """AUD02-06: a truncated run.yaml can parse as a valid partial dict
    (``{'queue': ['TODO-00']}``) — without shape validation the reader
    returns it as normal state: ``active`` lost, queue replaced by a
    garbage id, the rest of the queue silently gone.

    Valid shape: queue is a non-empty list of ``TODO-NNNN`` ids, index
    (when present) is an int in ``[0, len(queue)]`` (``len`` = exhausted),
    active (when present) is a bool. Absent index/active is tolerated —
    a crash-truncated file must degrade to "no run", not to a half-run.
    """
    queue = state.get("queue")
    if not isinstance(queue, list) or not queue:
        return False
    for q in queue:
        if not isinstance(q, str) or not _TODO_ID_RE.match(q):
            return False
    idx = state.get("index")
    if idx is not None and (
        isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx <= len(queue)
    ):
        return False
    active = state.get("active")
    if active is not None and not isinstance(active, bool):
        return False
    return True


def read_run(project_dir: Path, *, logs_dir: Path | None = None) -> dict | None:
    """Read run state. Returns None when no run was ever started or the
    file is corrupt (non-UTF-8, broken YAML, non-dict root, bad shape)."""
    f = run_file(project_dir)
    if not f.is_file():
        return None
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        # AUD12-11: non-UTF-8 bytes in run.yaml are a corrupt file —
        # degrade to "no run", not traceback (same as broken YAML).
        return None
    if not isinstance(data, dict):
        return None
    if not _run_shape_ok(data):
        # The run is lost — make it visible (the file itself is kept).
        if logs_dir is not None:
            _log(
                logs_dir,
                "AUD02-06: run.yaml shape invalid "
                "(queue/index/active) — treating as no run "
                "(corrupt file kept for inspection)",
            )
        return None
    return data


def _ensure_lock_file(project_dir: Path) -> None:
    """AUD05-05 (QA): the .lock must hold at least one byte before first use.

    ``locked()`` creates the lock file empty via ``open(..., "a+b")`` on
    first use; msvcrt.locking (Windows) locks by byte offset, and locking
    byte 0 of an empty file is unstable. Writing one byte up front — only
    when the file is absent — makes the Windows path stable. The Linux path
    is unchanged: flock does not depend on file contents.
    """
    state_dir = paths.agentic_dir(project_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = state_dir / ".lock"
    if lock_file.is_file():
        return
    try:
        with open(lock_file, "xb") as f:
            f.write(b"\0")
    except FileExistsError:
        pass  # a concurrent caller created it first


def write_run(project_dir: Path, **fields) -> dict:
    """Merge fields into run state and return the merged dict.

    AUD05-05: the read → merge → write runs under an advisory lock so
    concurrent write_run calls (parallel approve/reject, run_next +
    run_finish) cannot lose each other's updates.
    """
    from ._lock import locked

    _ensure_lock_file(project_dir)
    with locked(project_dir):
        state = read_run(project_dir) or {}
        state.update(fields)
        atomic_write_text(
            run_file(project_dir),
            yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
        )
    return state


def update_run(project_dir: Path, mutator) -> dict:
    """Atomic read → mutate → write in ONE lock hold (AUD05-05, rest).

    ``write_run`` serializes its own RMW, but callers like
    ``approve_commit`` / ``reject_commit`` / ``run_next`` used to read the
    state BEFORE the lock and pass whole keys (``outcomes``, ``rejects``,
    ``index``/``current``) computed from that stale snapshot — the shallow
    merge then clobbered a concurrent writer's update (supervisor repro:
    verdict='approved' while rejects>=1). ``update_run`` moves the
    computation INSIDE the lock: ``mutator`` receives the state exactly as
    it is on disk at that moment and returns the state to write (mutating
    the dict in place is enough; returning ``None`` keeps it). A mutator
    that finds a conflict (e.g. an approve that sees the TODO already
    rejected) returns the input unchanged — the audit invariant
    ``'approved' ⇒ rejects == 0`` is enforced by the caller, not by hope.

    Returns the written state.
    """
    from ._lock import locked

    _ensure_lock_file(project_dir)
    with locked(project_dir):
        state = read_run(project_dir) or {}
        new_state = mutator(state)
        if new_state is None:
            new_state = state
        if not isinstance(new_state, dict):
            raise TypeError(
                "update_run mutator must return a dict (or None) — "
                f"got {type(new_state).__name__}"
            )
        atomic_write_text(
            run_file(project_dir),
            yaml.safe_dump(new_state, allow_unicode=True, sort_keys=False),
        )
    return new_state


def clear_run(project_dir: Path) -> None:
    """Remove the run state file (soft reset)."""
    try:
        run_file(project_dir).unlink()
    except FileNotFoundError:
        pass


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_started_at(started: str) -> datetime | None:
    """Parse started_at ('%Y-%m-%dT%H:%M:%SZ'). None when absent/unparseable.

    ``str`` coercion first: YAML parses an unquoted date as a date object,
    and strptime on a non-str raises TypeError (AUD02-09).
    """
    try:
        return datetime.strptime(str(started), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, TypeError):
        return None


def started_at_ok(state: dict) -> bool:
    """AUD02-09: is the run clock trustworthy?

    Absent/empty started_at is NOT corrupt (fresh state, no clock yet) —
    the honest 0.0 case. A PRESENT but unparseable value is corrupt: the
    budget gate must degrade to a stop (see awf/api/run.py), not to
    "elapsed 0.0" which silently disables the budget.
    """
    started = state.get("started_at")
    if started is None or str(started).strip() == "":
        return True
    return _parse_started_at(started) is not None


def elapsed_minutes(state: dict) -> float:
    """Minutes since the run started (0.0 when started_at is absent or
    unparseable — display/degradation value. The BUDGET GATE must not rely
    on this: use :func:`started_at_ok` to stop on a corrupt clock
    (AUD02-09)."""
    started = state.get("started_at") or ""
    dt = _parse_started_at(started)
    if dt is None:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


def downtime_minutes(state: dict) -> float:
    """B2: recorded non-working minutes (checkpoint waits, salvage handling,
    net-backoff pauses). Defensive: a corrupt value degrades to 0.0."""
    try:
        return float(state.get("downtime_seconds", 0) or 0) / 60.0
    except (TypeError, ValueError):
        return 0.0


def productive_minutes(state: dict) -> float:
    """B2: elapsed minus recorded downtime, clamped at 0.

    The budget gate counts THIS, not wall clock: a run that spent an hour on
    a checkpoint timeout and three silent worker deaths is not one hour of
    autonomous work. (A corrupt clock still stops the run via
    :func:`started_at_ok` — downtime never resurrects a dead clock.)
    """
    return max(0.0, elapsed_minutes(state) - downtime_minutes(state))


def add_downtime(
    project_dir: Path,
    seconds: float,
    reason: str = "",
    logs_dir: Path | None = None,
) -> None:
    """B2: accumulate ``seconds`` into the ACTIVE run's ``downtime_seconds``.

    No-op when seconds is not a positive number, when there is no run state
    (absent or corrupt), or when the run is not active — a pipeline outside
    a run must not create or touch run.yaml (hard rule: behavior without an
    active run is unchanged).

    The read-then-write is deliberate: ``update_run`` overwrites the file
    with an empty dict when ``read_run`` returns None, which would destroy a
    corrupt run.yaml kept for inspection (AUD02-06).
    """
    try:
        secs = float(seconds)
    except (TypeError, ValueError):
        return
    if secs <= 0:
        return
    if read_run(project_dir) is None:
        return

    def _mut(state: dict) -> dict:
        if not state.get("active"):
            return state
        try:
            current = float(state.get("downtime_seconds", 0) or 0)
        except (TypeError, ValueError):
            current = 0.0
        state["downtime_seconds"] = current + secs
        return state

    update_run(project_dir, _mut)
    if logs_dir is not None:
        suffix = f" ({reason})" if reason else ""
        _log(logs_dir, f"B2: downtime +{secs:.0f}s{suffix}")


def position(state: dict) -> str:
    """Human position like ``1/3`` — the RUNNING item's number.

    NEG-2026-09-19 R4: ``run_next`` advances ``index`` at LAUNCH, so
    ``index + 1`` showed the NEXT item (2/4 while the first was running).
    When ``current`` is set, the running item is ``index``; between items
    (no current) the next-to-run is ``index + 1``.
    """
    queue = state.get("queue") or []
    total = len(queue)
    if not total:
        return "0/0"
    idx = int(state.get("index", 0) or 0)
    current = str(state.get("current") or "")
    shown = idx if current else idx + 1
    shown = max(1, min(shown, total))
    return f"{shown}/{total}"
