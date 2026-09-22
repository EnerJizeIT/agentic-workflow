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
- :func:`retire_todo` archives a rejected/abandoned ACTIVE TODO (battle
  case TODO-0035: reject leaves ``DONE-<id>.{md,json}`` without the
  ``.ready`` signal, so the TODO stays "active" forever) — the files move
  to ``done/<id>/`` with a ``RETIRED-<ts>.md`` note; no DONE signal is
  written, and ``awf restore`` still works.
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
from ..pipeline_state import read_state
from ..signals import short_id
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import RemoveTodoResult, RetireTodoResult, UnblockResult

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


def _unique_name(dest_dir: Path, name: str) -> str:
    """A name that does not exist in ``dest_dir`` yet (never overwrite).

    ``PROGRESS-TODO-0001.md`` → ``PROGRESS-TODO-0001.md``, then
    ``PROGRESS-TODO-0001-1.md``, ``PROGRESS-TODO-0001-2.md``, ...
    """
    candidate = name
    stem, dot, ext = name.partition(".")
    i = 1
    while (dest_dir / candidate).exists():
        candidate = f"{stem}-{i}.{ext}" if dot else f"{name}-{i}"
        i += 1
    return candidate


def retire_todo(project_dir: Path, todo_id: str, reason: str) -> RetireTodoResult:
    """Archive a rejected/abandoned ACTIVE TODO — no fake DONE signal.

    RUN5 #2 (battle case TODO-0035): the reject path writes
    ``DONE-<id>.{md,json}`` / ``PROGRESS-<id>.md`` / ``REVIEW-<id>.md`` to
    the outbox but NOT ``DONE-<id>.ready`` — ``todos.is_closed`` stays
    false, so ``awf status`` / ``awf brief`` keep listing the TODO as
    active forever. Neither ``unblock`` (only BLOCKED/ACK), ``todo-remove``
    (refuses with ``.ready``) nor ``reset --orphans`` (it has PROGRESS)
    covers this state.

    Moves the TODO's files to ``done/<id>/`` — the same archive
    ``awf restore`` reads, so the recovery path survives:

    - ``inbox/TODO-<id>.md``      → ``done/<id>/TODO.md`` (restore shape)
    - ``inbox/TODO-<id>.ready``   → ``done/<id>/`` (kept, not deleted)
    - ``outbox/PROGRESS-<id>.*``  → ``done/<id>/``
    - ``outbox/DONE-<id>.{md,json}`` (NOT ``.ready``) → ``done/<id>/``
    - ``outbox/REVIEW-<id>.*``    → ``done/<id>/``

    (canonical and legacy short id forms). Writes
    ``done/<id>/RETIRED-<timestamp>.md`` with the reason, who (supervisor),
    time and the moved-file list, and logs to the orchestrator log.

    Refusals (clear errors, nothing moved):
    - no ``TODO-<id>.md`` in inbox and no ``done/<id>/TODO.md`` — not found;
    - ``done/<id>/TODO.md`` exists — already archived (use ``awf restore``);
    - a live pipeline on this id (PID alive + ``state/current.yaml``
      ``todo_id``) — ``awf kill`` or wait first;
    - empty ``reason`` — required (it becomes the RETIRED note body).
    """
    _validate_id(todo_id)
    reason = (reason or "").strip()
    if not reason:
        raise AwfApiError(
            "reason is required — it is written to the RETIRED note "
            "(why the TODO is retired)"
        )
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    md = inbox / f"{todo_id}.md"
    done_dir = paths.done_dir(project_dir) / todo_id
    archived_md = done_dir / "TODO.md"

    if archived_md.is_file():
        extra = (
            f" and a second copy sits in inbox/{md.name} — inspect manually"
            if md.is_file()
            else ""
        )
        raise AwfApiError(
            f"{todo_id} is already archived (done/{todo_id}/TODO.md{extra}) — "
            "use awf restore to bring an archived TODO back."
        )
    if not md.is_file():
        raise AwfApiError(
            f"{todo_id}.md not found in inbox and no done/{todo_id}/TODO.md — "
            "nothing to retire."
        )

    # Live pipeline on THIS id: the engine owns the files right now.
    # A pipeline on another id does not touch this TODO's files — allowed.
    running, pid, _tail = check_pipeline_running(project_dir)
    if running:
        state = read_state(project_dir)
        if isinstance(state, dict) and state.get("todo_id") == todo_id:
            raise AwfApiError(
                f"a live pipeline is running on {todo_id} (pid {pid}) — "
                "wait for it to finish or awf kill first"
            )

    short = short_id(todo_id)
    ids = (todo_id, short) if short != todo_id else (todo_id,)

    # (source, archive name) pairs — inbox first, then outbox artifacts.
    candidates: list[tuple[Path, str]] = [(md, "TODO.md")]
    ready = inbox / f"{todo_id}.ready"
    if ready.is_file():
        candidates.append((ready, ready.name))
    for tid in ids:
        candidates.extend(
            (p, p.name) for p in sorted(outbox.glob(f"PROGRESS-{tid}.*")) if p.is_file()
        )
        for ext in (".md", ".json"):
            p = outbox / f"DONE-{tid}{ext}"
            if p.is_file():
                candidates.append((p, p.name))
        candidates.extend(
            (p, p.name) for p in sorted(outbox.glob(f"REVIEW-{tid}.*")) if p.is_file()
        )

    done_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    pairs: list[tuple[str, str]] = []  # (from, to) — project-relative
    for src, name in candidates:
        target = done_dir / _unique_name(done_dir, name)
        shutil.move(str(src), str(target))
        to_rel = target.relative_to(project_dir).as_posix()
        moved.append(to_rel)
        pairs.append((src.relative_to(project_dir).as_posix(), to_rel))

    ts = _timestamp()
    note = done_dir / f"RETIRED-{ts}.md"
    note_lines = [
        f"# RETIRED: {todo_id}",
        "",
        f"- when: {ts}",
        "- by: supervisor (awf todo-retire)",
        f"- reason: {reason}",
        "",
        "## Moved to archive",
        "",
    ]
    note_lines += [f"- {src} → {to}" for src, to in pairs]
    note_lines.append("")
    note.write_text("\n".join(note_lines), encoding="utf-8")
    note_rel = note.relative_to(project_dir).as_posix()

    _log(
        paths.logs_dir(project_dir),
        f"retire: {todo_id} — {len(moved)} file(s) → "
        f"done/{todo_id}/ (note: RETIRED-{ts}.md, reason: {reason})",
    )
    return RetireTodoResult(
        todo_id=todo_id,
        moved=moved,
        retired_note=note_rel,
        message=(
            f"{todo_id} retired: {len(moved)} file(s) → done/{todo_id}/ "
            f"(note: RETIRED-{ts}.md)."
        ),
    )


__all__ = [
    "closure_files",
    "clear_stale_closures",
    "has_done_closure",
    "remove_todo",
    "retire_todo",
    "unblock_todo",
]
