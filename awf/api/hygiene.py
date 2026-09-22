"""TODO state hygiene — stale-closure clearing and never-started removal.

RUN3 #4/#5: after an interrupted run a stale ``outbox/BLOCKED-<id>.ready``
keeps ``todos.is_closed`` true, so a re-issued TODO is invisible to
``awf_status`` / ``awf_start``. These helpers make the two repairs explicit
operations that leave a trace:

- :func:`unblock_todo` moves BLOCKED/ACK closure signals (canonical and
  legacy forms) into a timestamped ``context/`` directory. DONE signals are
  never touched — an archived TODO comes back only via ``awf restore``.
- :func:`remove_todo` deletes a TODO that never started (no ``.ready``, no
  signals, no progress); the file moves to ``done/<id>/removed-<ts>.md``.
- :func:`clear_stale_closures` is the shared helper — ``dispatch_todo``
  calls it automatically on re-dispatch of the same number.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import paths, todos
from .._log import log as _log
from ..signals import short_id
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import RemoveTodoResult, UnblockResult

_ID_RE = re.compile(r"^TODO-\d{4,}$")


def _validate_id(todo_id: str) -> None:
    if not _ID_RE.match(todo_id or ""):
        raise AwfApiError(f"invalid todo_id {todo_id!r}, expected TODO-NNNN")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def closure_files(project_dir: Path, todo_id: str) -> list[Path]:
    """All BLOCKED/ACK closure signal files for ``todo_id`` (canonical + legacy).

    Mirrors ``todos.is_closed`` file set (plus the ``.md`` companions and
    the BD-21 ``.md.ready`` typo form the engine also accepts). DONE-* is
    intentionally excluded: a DONE closure is lifted only by ``awf restore``.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    short = short_id(todo_id)
    candidates = [
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"BLOCKED-{todo_id}.md",
        outbox / f"BLOCKED-{todo_id}.md.ready",
        inbox / f"ACK-{todo_id}.ready",
    ]
    if short != todo_id:
        candidates += [
            outbox / f"BLOCKED-{short}.ready",
            outbox / f"BLOCKED-{short}.md",
            outbox / f"BLOCKED-{short}.md.ready",
            inbox / f"ACK-{short}.ready",
        ]
    return [p for p in candidates if p.is_file()]


def has_done_closure(project_dir: Path, todo_id: str) -> bool:
    """True if a DONE-* closure signal (canonical or legacy) is in the outbox."""
    short = short_id(todo_id)
    outbox = paths.outbox(project_dir)
    for tid in dict.fromkeys((todo_id, short)):
        if (outbox / f"DONE-{tid}.ready").is_file():
            return True
    return False


def clear_stale_closures(project_dir: Path, todo_id: str) -> tuple[list[str], Path | None]:
    """Move all BLOCKED/ACK closures of ``todo_id`` into a trace dir.

    Destination: ``context/unblock-<todo_id>-<timestamp>/`` — each call gets
    its own directory, so repeated unblocks never overwrite an earlier trace.
    Returns ``(moved basenames, trace dir)`` — both empty/None when there is
    nothing to move. DONE-* signals are never moved.
    """
    files = closure_files(project_dir, todo_id)
    if not files:
        return [], None
    trace = paths.context_dir(project_dir) / f"unblock-{todo_id}-{_timestamp()}"
    trace.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for f in files:
        shutil.move(str(f), str(trace / f.name))
        moved.append(f.name)
    return moved, trace


def unblock_todo(project_dir: Path, todo_id: str) -> UnblockResult:
    """Clear stale BLOCKED/ACK closures so the TODO is active again.

    RUN3 #4: the manual repair for the TODO-0018 case — a stale
    ``BLOCKED-<id>.ready`` outlived a re-issue and hid the fresh TODO from
    ``awf_status``. Refused while the pipeline is running (moving a signal
    out from under a live engine would break its wait).
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    running, _pid, _tail = check_pipeline_running(project_dir)
    if running:
        raise AwfApiError(
            "pipeline is running — wait for verify/salvage, or awf kill first"
        )

    moved, trace = clear_stale_closures(project_dir, todo_id)
    if not moved:
        raise AwfApiError(
            f"No closure signals (BLOCKED/ACK) for {todo_id} — nothing to unblock. "
            "DONE closures are never lifted by unblock; use awf restore for an "
            "archived TODO."
        )

    trace_rel = trace.relative_to(project_dir).as_posix() if trace else ""
    _log(
        paths.logs_dir(project_dir),
        f"unblock: {todo_id} — cleared {', '.join(moved)} → {trace_rel}",
    )
    return UnblockResult(
        todo_id=todo_id,
        moved=moved,
        trace=trace_rel,
        message=(
            f"{todo_id} unblocked: {len(moved)} stale closure signal(s) "
            f"moved to {trace_rel}."
        ),
    )


def remove_todo(project_dir: Path, todo_id: str) -> RemoveTodoResult:
    """Delete a TODO that never started; the file keeps a trace in done/.

    RUN3 #5: an inert TODO (``.md`` present, no ``.ready``, no signals, no
    progress) confuses ``awf status`` and blocks nothing — but it must not
    be erased silently either, so the file moves to
    ``done/<id>/removed-<timestamp>.md``. Refused when the TODO shows any
    sign of having been started (``.ready``, outbox signals, inbox
    ACK/APPROVE, progress) or while the pipeline is running.
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    running, _pid, _tail = check_pipeline_running(project_dir)
    if running:
        raise AwfApiError(
            "pipeline is running — wait for verify/salvage, or awf kill first"
        )

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    md = inbox / f"{todo_id}.md"
    if not md.is_file():
        raise AwfApiError(f"{todo_id}.md not found in inbox — nothing to remove.")

    short = short_id(todo_id)
    if (inbox / f"{todo_id}.ready").is_file():
        raise AwfApiError(
            f"{todo_id} has a dispatch signal ({todo_id}.ready) — it was armed "
            "for the pipeline. Use awf unblock or awf reset --orphans."
        )

    ids = (todo_id, short) if short != todo_id else (todo_id,)
    outbox_signals = [
        p
        for tid in ids
        for p in sorted(outbox.glob(f"*-{tid}.*"))
        if p.is_file()
    ]
    inbox_signals = [
        p
        for tid in ids
        for prefix in ("ACK-", "APPROVE-")
        if (p := inbox / f"{prefix}{tid}.ready").is_file()
    ]
    if outbox_signals or inbox_signals:
        names = ", ".join(p.name for p in outbox_signals + inbox_signals)
        if any(p.name.startswith("DONE-") for p in outbox_signals):
            hint = "DONE — the TODO is closed as finished; use awf restore."
        else:
            hint = "stale BLOCKED/ACK — use awf unblock; orphans — awf reset --orphans."
        raise AwfApiError(
            f"{todo_id} has signals: {names} — it was started. {hint}"
        )
    if todos.has_progress(outbox, todo_id):
        raise AwfApiError(
            f"{todo_id} has PROGRESS — it was started. Use awf reset --orphans."
        )

    done_dir = paths.done_dir(project_dir) / todo_id
    done_dir.mkdir(parents=True, exist_ok=True)
    trace = done_dir / f"removed-{_timestamp()}.md"
    shutil.move(str(md), str(trace))
    trace_rel = trace.relative_to(project_dir).as_posix()
    _log(paths.logs_dir(project_dir), f"todo-remove: {todo_id} — {md.name} → {trace_rel}")
    return RemoveTodoResult(
        todo_id=todo_id,
        trace_path=trace_rel,
        message=f"{todo_id} removed (never started). Trace: {trace_rel}.",
    )


__all__ = [
    "closure_files",
    "clear_stale_closures",
    "has_done_closure",
    "remove_todo",
    "unblock_todo",
]
