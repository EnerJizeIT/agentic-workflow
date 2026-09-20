"""T4.1: Pipeline state persistence.

Writes structured state to ``.agentic/state/current.yaml`` after each
pipeline transition. Replaces regex log parsing in
:func:`awf.api.context._extract_stage_info` — single source of truth,
no fragile regex on ``print()`` format.

State is written by orchestrator at these points:
- Pipeline start (clear previous state)
- Each stage start (stage_idx, stage_name, stage_kind)
- BD-36 checkpoint opened (checkpoint_pending=True, form_url, port)
- BD-36 checkpoint resolved (checkpoint_pending=False)
- Worker DONE detected (last_signal=DONE-TODO-NNNN)
- Worker BLOCKED detected (last_signal=BLOCKED-TODO-NNNN)
- REVIEW written (last_signal=REVIEW-TODO-NNNN)
- Pipeline exit (clear checkpoint_pending, set pipeline_running=False)

Readers (``api.get_status``, ``api.load_supervisor_context``) read this
file first, fall back to regex parsing if file missing/stale.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import paths
from ._atomic import atomic_write_text
from ._log import log as _log

STATE_DIR_NAME = "state"
STATE_FILE_NAME = "current.yaml"


def _state_file(project_dir: Path) -> Path:
    """Return path to .agentic/state/current.yaml."""
    return paths.agentic_dir(project_dir) / STATE_DIR_NAME / STATE_FILE_NAME


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
        **fields: keys to set (stage_idx, stage_name, stage_kind,
            started_at, last_signal, checkpoint_pending,
            checkpoint_form_url, checkpoint_port, todo_id, pipeline_pid).

    Example::

        write_state(project_dir, stage_name="agent-developer",
                    stage_kind="execute", stage_idx=2)
    """
    state_path = _state_file(project_dir)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    # AUD05-05: merge is read-modify-write — serialize with the same
    # advisory lock as run_state.write_run so concurrent stage
    # transitions don't lose each other's fields.
    from ._lock import locked

    try:
        with locked(project_dir):
            # Merge with existing state
            current = read_state(project_dir) or {}
            current.update(fields)
            # Always update `updated_at` for staleness detection
            current["updated_at"] = datetime.now(timezone.utc).isoformat()

            content = yaml.safe_dump(
                current, default_flow_style=False, allow_unicode=True, sort_keys=True
            )
            atomic_write_text(state_path, content)
    except OSError as e:
        if logs_dir is not None:
            _log(logs_dir, f"T4.1: state write failed: {e}")
        # Non-fatal — pipeline continues, readers fall back to regex


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
    except (yaml.YAMLError, OSError):
        return None


def clear_state(project_dir: Path, *, logs_dir: Path | None = None) -> None:
    """Remove state file (called on pipeline exit / abort).

    Leaves the file absent so readers know pipeline is not running.
    """
    state_path = _state_file(project_dir)
    try:
        if state_path.is_file():
            state_path.unlink()
    except OSError as e:
        if logs_dir is not None:
            _log(logs_dir, f"T4.1: state clear failed: {e}")


def is_state_stale(state: dict[str, Any], max_age_seconds: int = 7200) -> bool:
    """Check if state file is older than ``max_age_seconds`` (default 2h).

    Readers use this to avoid trusting stale state from a crashed pipeline
    whose orchestrator never wrote a clean exit. If stale, fall back to
    regex parsing or report pipeline as not running.
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


__all__ = ["write_state", "read_state", "clear_state", "is_state_stale"]
