"""Port of lib/todos.sh — shared helpers for enumerating TODOs."""
from __future__ import annotations

from pathlib import Path

from .signals import short_id as _short_id


def is_closed(inbox: Path, outbox: Path, todo_id: str) -> bool:
    """Return True if any closure signal exists (canonical OR legacy form).

    Canonical: DONE-TODO-NNNN.ready, BLOCKED-TODO-NNNN.ready, ACK-TODO-NNNN.ready
    Legacy:    DONE-NNNN.ready,       BLOCKED-NNNN.ready,       ACK-NNNN.ready
    """
    short = _short_id(todo_id)
    closure_patterns = [
        outbox / f"DONE-{todo_id}.ready",
        outbox / f"DONE-{short}.ready",
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"BLOCKED-{short}.ready",
        inbox / f"ACK-{todo_id}.ready",
        inbox / f"ACK-{short}.ready",
    ]
    return any(p.exists() for p in closure_patterns)


def has_progress(outbox: Path, todo_id: str) -> bool:
    """Return True if a non-empty PROGRESS file exists (canonical OR legacy)."""
    short = _short_id(todo_id)
    progress_patterns = [
        outbox / f"PROGRESS-{todo_id}.ready",
        outbox / f"PROGRESS-{short}.ready",
        outbox / f"PROGRESS-{todo_id}.md",
        outbox / f"PROGRESS-{short}.md",
    ]
    return any(p.exists() and p.stat().st_size > 0 for p in progress_patterns)


def list_active_todos(inbox: Path, outbox: Path) -> list[str]:
    """Return active TODO ids, highest-numbered first.

    Active = .ready exists, .md non-empty, no closure signal.
    """
    if not inbox.exists():
        return []

    candidates: list[tuple[int, str]] = []
    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem  # e.g. TODO-0042
        md_file = inbox / f"{todo_id}.md"
        if not md_file.is_file() or md_file.stat().st_size == 0:
            continue
        if is_closed(inbox, outbox, todo_id):
            continue
        short = _short_id(todo_id)
        try:
            num = int(short)
        except ValueError:
            num = 0
        candidates.append((num, todo_id))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [todo_id for _, todo_id in candidates]


def newest_active(project_root: str | Path) -> str:
    """Return the highest-numbered active TODO id, or empty string if none.

    Convenience wrapper around ``list_active_todos`` for callers that just
    need "the current TODO" without managing inbox/outbox paths themselves.
    Used by orchestrator and cmd_start.
    """
    from . import paths

    project_root = Path(project_root)
    inbox = paths.inbox(project_root)
    outbox = paths.outbox(project_root)
    active = list_active_todos(inbox, outbox)
    return active[0] if active else ""
