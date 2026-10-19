"""T4.1: Pipeline state persistence.

Writes structured state to ``.agentic/state/current.yaml`` after each
pipeline transition. Single source of truth — the regex log fallback was
removed in AUD-12.5 (c9d88b9); a missing or stale state degrades readers
to "no pipeline info", never to a second parsing path.

State is written at these points:
- Each stage start (stage_idx, stage_name, stage_kind, todo_id,
  pipeline_pid + clears the previous run's salvage_needed/salvage_stage,
  last_signal and checkpoint_* keys — AUD02-03)
- Plan/verify/execute transitions (phase, todo_id, last_signal)
- Worker signal classified (last_signal=DONE/BLOCKED/...-TODO-NNNN,
  salvage counters reset)
- BD-36 checkpoint opened (checkpoint_pending=True, form_url, port)
- BD-36 checkpoint resolved or timed out (checkpoint_pending=False,
  port/url cleared)
- Pipeline exit (state cleared; phase=done + goal/normalized preserved —
  AUD02-04)
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import paths
from ._log import log as _log
from .infra import state_store

STATE_FILE_NAME = "current.yaml"


def _state_file(project_dir: Path) -> Path:
    """Return path to .agentic/state/current.yaml."""
    # R1.5 (arch-review): the ``state/`` component has ONE owner
    # (``paths.state_dir``) — this facade must not re-spell it (the
    # divergent-convention class the iter-3 accessors closed). ``STATE_FILE_NAME``
    # stays the leaf's owner here.
    return paths.state_dir(project_dir) / STATE_FILE_NAME


def write_state(
    project_dir: Path,
    *,
    logs_dir: Path | None = None,
    **fields: Any,
) -> None:
    """Update pipeline state file (merge with existing fields).

    Atomic write via temp+rename. Missing fields preserved from previous
    state (so callers don't have to repeat values they didn't change).

    Args:
        project_dir: awf project root.
        logs_dir: optional, for logging write failures.
        **fields: keys to set (stage_idx, stage_name, stage_kind, todo_id,
            pipeline_pid, pipeline, phase, goal, normalized, last_signal,
            checkpoint_pending, checkpoint_form_url, checkpoint_port,
            salvage_needed, salvage_stage, ...). ``pipeline`` is the name
            of the ACTUAL (resolved) pipeline the run was launched with —
            the dashboard draws its stages, not the default pipeline's
            (RUN6 #2). Note: ``started_at`` lives in run.yaml (run_state),
            NOT here.

    Example::

        write_state(project_dir, stage_name="agent-developer",
                    stage_kind="execute", stage_idx=2)
    """

    def _merge(current: dict[str, Any] | None) -> dict[str, Any]:
        merged = dict(current or {})
        merged.update(fields)
        # Always update `updated_at` for staleness detection
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        return merged

    _mutate_state_locked(
        project_dir, _merge, logs_dir=logs_dir, failure_label="state write"
    )


def drop_fields(
    project_dir: Path,
    *keys: str,
    logs_dir: Path | None = None,
) -> None:
    """Remove ``keys`` from the state file (a merge cannot delete a key).

    R1.5: the stale-PID cleanup in ``api/pipeline._reconcile`` used to dump
    ``current.yaml`` from the api layer because :func:`write_state` only
    merges. The removal now goes through the file's OWNER, so the state-file
    bytes still leave only via the Store (single writer).

    ``updated_at`` is deliberately NOT refreshed: a key removal is house-
    keeping, not a stage transition, and bumping the timestamp would mask
    staleness for the very cleanup that needs it. A missing file or key is a
    no-op.
    """

    def _drop(current: dict[str, Any] | None) -> dict[str, Any] | None:
        if current is None:
            return None
        for key in keys:
            current.pop(key, None)
        return current

    _mutate_state_locked(
        project_dir, _drop, logs_dir=logs_dir, failure_label="state key removal"
    )


def _mutate_state_locked(
    project_dir: Path,
    mutator: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    *,
    logs_dir: Path | None,
    failure_label: str,
) -> None:
    """Read-modify-write ``current.yaml`` under the advisory lock (single writer).

    ``mutator`` receives the current state (``None`` when the file is absent
    or corrupt) and returns the mapping to publish, or ``None`` to leave the
    file untouched (no write — the "nothing to do" case of
    :func:`drop_fields`). The Store publishes the bytes; an ``OSError`` —
    and, since R1.5 moved the writer to the managed no-follow writer, a
    ``ManagedPathError`` (a symlinked ``.agentic`` / ``state`` reports it) —
    is logged via ``logs_dir`` (when given) and swallowed: a state write is
    non-fatal for the pipeline, which degrades to "no state".

    AUD05-05: the merge is read-modify-write, so it serializes with the same
    advisory lock as ``run_state.write_run`` to keep concurrent stage
    transitions from losing each other's fields.
    """
    state_path = _state_file(project_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    from ._lock import locked

    try:
        with locked(project_dir):
            new_state = mutator(read_state(project_dir))
            if new_state is None:
                return
            content = yaml.safe_dump(
                new_state, default_flow_style=False, allow_unicode=True, sort_keys=True
            )
            # R1.5: single writer — the Store publishes the projection bytes.
            state_store.write_state_file(state_path, content)
    except state_store.STATE_WRITE_FAILURES as e:
        # R2.5 REVIEW (iter 2 / L1): the whole failure family is non-fatal —
        # see ``state_store.STATE_WRITE_FAILURES``. R1.5 moved this writer onto the
        # managed no-follow writer, which refuses a symlinked ``.agentic`` /
        # ``state`` with ``ManagedPathError`` (NOT an OSError); the lock adds
        # ``LockUnavailable`` on a platform without flock. The barrier stays —
        # nothing escapes the project — but the documented contract holds:
        # log loudly and degrade to "no state" instead of crashing the stage
        # transition.
        if logs_dir is not None:
            _log(logs_dir, f"T4.1: {failure_label} failed: {e}")


def read_state(project_dir: Path) -> dict[str, Any] | None:
    """Read pipeline state from disk. Returns None if missing/corrupt."""
    state_path = _state_file(project_dir)
    if not state_path.is_file():
        return None
    try:
        data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
        return None
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        # AUD12-11: a state file with non-UTF-8 bytes is corrupt the same
        # way a broken YAML one is — degrade to "no state", not traceback.
        return None


def clear_state(project_dir: Path, *, logs_dir: Path | None = None) -> None:
    """Remove state file (called on pipeline exit / abort).

    Leaves the file absent so readers know pipeline is not running.

    R1.5 (REVIEW A1): a real failure to remove the file is NOT swallowed —
    it propagates like :func:`run_state.clear_run`. ``reset`` needs the
    signal to report ``partial_failure`` (a swallowed error used to leave
    ``state_cleared=True`` next to a ghost ``current.yaml`` that blocked the
    next run). A caller that wants best-effort logs it via ``logs_dir`` and
    catches the error itself. A missing file is a no-op.

    R2.5 REVIEW (iter 6, E1): the propagated refusal is always the
    ``OSError`` family — a ``ManagedPathError`` (symlinked ``.agentic`` /
    ``state``) or a ``LockUnavailable`` (no flock) is re-raised as ``OSError``
    with its cause, so the documented ``except OSError`` callers keep their
    degradation path (the clean exit, the kill path, the close gate).
    """
    state_path = _state_file(project_dir)
    try:
        if state_path.is_file():
            # R1.5: the removal is a state-file write too — the Store owns it.
            state_store.remove_state_file(state_path)
    except state_store.STATE_WRITE_FAILURES as e:
        # R2.5 REVIEW (iter 6, E1): the removal shares the SAME failure family
        # as the writer (``state_store.STATE_WRITE_FAILURES``) — a symlinked
        # ``.agentic`` / ``state`` makes ``remove_state_file`` raise
        # ``ManagedPathError``, which a bare ``except OSError`` used to swallow
        # without a trace. Unlike the writer this path still PROPAGATES
        # (``reset`` needs the failure to report ``partial_failure``), so the
        # non-OSError members cross the boundary AS ``OSError`` with their
        # cause. The one translation lives next to the one list
        # (``state_store.raise_state_removal_failure``) — R2.5
        # architector-review, no copy here.
        if logs_dir is not None:
            _log(logs_dir, f"T4.1: state clear failed: {e}")
        state_store.raise_state_removal_failure(e, "clear state")


def is_state_stale(state: dict[str, Any], max_age_seconds: int = 7200) -> bool:
    """Check if state file is older than ``max_age_seconds`` (default 2h).

    Readers use this to avoid trusting stale state from a crashed pipeline
    whose orchestrator never wrote a clean exit.
    """
    updated_at = state.get("updated_at")
    if not updated_at:
        return True
    try:
        # Handle both 'Z' suffix (3.11+) and '+00:00' (3.9+)
        ts_str = str(updated_at).replace("Z", "+00:00")
        ts = datetime.fromisoformat(ts_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        return age > max_age_seconds
    except (ValueError, TypeError):
        return True


def state_trusted(project_dir: Path, state: dict[str, Any] | None) -> bool:
    """AUD02-12: may a reader trust this state?

    Fresh (not stale by timestamp) → trusted. A STALE timestamp is
    overridden by a LIVE pipeline: one stage longer than the 2h staleness
    window is not a crash — liveness is the PID (the shared resolver
    ``awf.api._liveness.resolve``), the ``updated_at`` age is not. Stale
    + dead pipeline keeps the old answer: not trusted (crash recovery).
    """
    if not state:
        return False
    if not is_state_stale(state):
        return True
    try:
        # Function-local: awf.api is the heavy package; the liveness
        # question only matters on the stale path (see AUD14-05 for the
        # core→api lazy-import convention).
        from .api._liveness import resolve

        return bool(resolve(project_dir)[0])
    except Exception:
        # Liveness check must never break a status read — degrade to the
        # conservative answer (not trusted).
        return False


__all__ = [
    "write_state",
    "drop_fields",
    "read_state",
    "clear_state",
    "is_state_stale",
    "state_trusted",
]
