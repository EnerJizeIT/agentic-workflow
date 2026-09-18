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
    except (OSError, yaml.YAMLError):
        return None
    return data if isinstance(data, dict) else None


def write_run(project_dir: Path, **fields) -> dict:
    """Merge fields into run state and return the merged dict."""
    state = read_run(project_dir) or {}
    state.update(fields)
    atomic_write_text(
        run_file(project_dir),
        yaml.safe_dump(state, allow_unicode=True, sort_keys=False),
    )
    return state


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
    """Human position like ``2/3`` (the item about to run / total)."""
    queue = state.get("queue") or []
    total = len(queue)
    if not total:
        return "0/0"
    idx = int(state.get("index", 0) or 0)
    if idx >= total:
        return f"{total}/{total}"
    return f"{idx + 1}/{total}"
