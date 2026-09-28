"""ORCH M4.2: stalled-stage detection (progress control).

The active pipeline stage can be quiet for a long time while a worker
runs a long command — nothing in between writes a state event. The
detection below makes that visible: one shared detector, rendered by the
surfaces that need it (the ``awf_brief`` card line, the ``awf_run_status``
message).

"Quiet" is measured against the STOCK timestamps, not a new store:
``current.yaml``'s ``updated_at`` (written on every stage start, signal
classification and checkpoint transition) and the stage's last
``Stage N:`` stamp in ``orchestrator.log`` (the same lines the
wait-timeout sizing reads). The line is a fact (stage, TODO, minutes),
not a verdict.

The threshold is ``run.stall_minutes`` in ``.agentic/config.yaml``:
absent or 0 means the warning is OFF (an opt-in diagnostic, not a gate).
Any file-state problem degrades to "no warning", never a traceback —
the surfaces are read-only info tools (M1 style).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths


@dataclass(frozen=True)
class StageStall:
    """The active pipeline stage has not produced a state event for a
    while: the stage name, its TODO and the quiet duration (minutes)."""

    stage: str
    todo_id: str
    minutes: int
    threshold_minutes: int


def stall_threshold_minutes(project_dir: Path | str) -> int:
    """The stall threshold from ``run.stall_minutes`` in
    ``.agentic/config.yaml``, in minutes.

    Absent, ``0``, negative or non-numeric → ``0`` (the warning is OFF —
    the stall line is an opt-in diagnostic, not a gate; a broken value
    degrades off, never a traceback, M1 style). A positive int → the
    threshold.
    """
    try:
        from . import config as _config

        value = _config.get(_config.load(project_dir), "run.stall_minutes")
    except Exception:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if value > 0 else 0


def _parse_updated_at(raw: object) -> datetime | None:
    """``current.yaml``'s ``updated_at`` (``isoformat`` with ``Z`` or
    ``+00:00``). None when absent/unparseable (degradation, M1 style)."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def detect_stage_stall(
    project_dir: Path | str, *, now: datetime | None = None
) -> StageStall | None:
    """The active pipeline stage that has been quiet for at least
    ``run.stall_minutes`` (None = no warning).

    None when: the threshold is off, there is no live stage marker
    (no state / no stage fields), or no timestamp survived. Read-only:
    any file-state problem degrades to None, never a traceback.
    """
    threshold = stall_threshold_minutes(project_dir)
    if threshold <= 0:
        return None
    try:
        from . import pipeline_state
        from ._log_reader import read_log_snapshot

        state = pipeline_state.read_state(project_dir)
        if not state:
            return None
        # The live-stage markers, same shape wait_event._state_describes_
        # live_pipeline uses: a marker-less leftover (phase=done, the
        # salvage counter) is not a running stage.
        if not (state.get("stage_kind") or state.get("stage_name") or state.get("todo_id")):
            return None
        stage = str(state.get("stage_name") or "").strip() or "?"
        todo = str(state.get("todo_id") or "").strip()

        stamps: list[datetime] = []
        updated = _parse_updated_at(state.get("updated_at"))
        if updated is not None:
            stamps.append(updated)
        log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
        for epoch, name in read_log_snapshot(log_file).transitions:
            if name == stage:
                stamps.append(datetime.fromtimestamp(epoch, tz=timezone.utc))
        if not stamps:
            return None
        if now is None:
            now = datetime.now(timezone.utc)
        minutes = int((now - max(stamps)).total_seconds() // 60)
        if minutes < threshold:
            return None
        return StageStall(
            stage=stage, todo_id=todo, minutes=minutes, threshold_minutes=threshold
        )
    except Exception:
        return None


def stall_line(stall: StageStall | None) -> str:
    """One line for a surface: ``X (TODO-0001) — quiet for 47 min
    (limit 30 min)``. ``""`` when there is no stall. Compact on purpose:
    the line names the stage, the TODO, the quiet duration — and the
    card's word budget (awf/brief.py MAX_WORDS) is paid by the verbose
    blocks, not by stretching this line (RUN6 #5 rule)."""
    if not stall:
        return ""
    todo = f" ({stall.todo_id})" if stall.todo_id else ""
    return (
        f"{stall.stage}{todo} — quiet for {stall.minutes} min "
        f"(limit {stall.threshold_minutes} min)"
    )


__all__ = [
    "StageStall",
    "stall_threshold_minutes",
    "detect_stage_stall",
    "stall_line",
]
