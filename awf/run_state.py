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

from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import paths
from ._atomic import atomic_write_text


def run_file(project_dir: Path) -> Path:
    """Path of the run state file."""
    return paths.agentic_dir(project_dir) / "state" / "run.yaml"


def read_run(project_dir: Path) -> dict | None:
    """Read run state. Returns None when no run was ever started."""
    f = run_file(project_dir)
    if not f.is_file():
        return None
    try:
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        # AUD12-11: non-UTF-8 bytes in run.yaml are a corrupt file —
        # degrade to "no run", not traceback (same as broken YAML).
        return None
    return data if isinstance(data, dict) else None


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


def elapsed_minutes(state: dict) -> float:
    """Minutes since the run started (0.0 when started_at is unparseable)."""
    started = state.get("started_at") or ""
    try:
        dt = datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0


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
