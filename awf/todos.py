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


def archive_todo(project_dir: str | Path, todo_id: str) -> Path | None:
    """DF6-1: Move completed TODO files from inbox/outbox to done/{todo_id}/.

    After verify approve (ACK/APPROVE), call this to archive:
    - inbox/TODO-{id}.md        → done/{id}/TODO.md
    - inbox/TODO-{id}.ready     → deleted (signal consumed)
    - inbox/ACK-{id}.ready      → deleted
    - inbox/APPROVE-{id}.ready  → deleted
    - outbox/PROGRESS-{id}.md   → done/{id}/PROGRESS.md
    - outbox/DONE-{id}.md       → done/{id}/DONE.md
    - outbox/DONE-{id}.ready    → deleted

    Returns path to done/{todo_id}/ dir, or None if nothing to archive.
    Idempotent — safe to call multiple times.
    """
    import shutil

    from . import paths

    project_dir = Path(project_dir)
    inbox_p = paths.inbox(project_dir)
    outbox_p = paths.outbox(project_dir)
    dest = paths.done_dir(project_dir) / todo_id

    moved_anything = False

    # Create dest dir
    dest.mkdir(parents=True, exist_ok=True)

    # Move TODO .md
    todo_md = inbox_p / f"{todo_id}.md"
    if todo_md.is_file():
        shutil.move(str(todo_md), str(dest / "TODO.md"))
        moved_anything = True

    # Delete consumed signal files from inbox.
    # NOTE: {todo_id} already contains "TODO-" prefix (e.g. "TODO-0001"),
    # so dispatch signal is {todo_id}.ready (not TODO-{todo_id}.ready).
    for pattern in [f"{todo_id}.ready", f"ACK-{todo_id}.ready", f"APPROVE-{todo_id}.ready"]:
        p = inbox_p / pattern
        if p.exists():
            p.unlink()
            moved_anything = True

    # Move PROGRESS and DONE from outbox
    for prefix in ("PROGRESS", "DONE"):
        for ext in (".md", ".ready"):
            src = outbox_p / f"{prefix}-{todo_id}{ext}"
            if src.is_file():
                if ext == ".md":
                    shutil.move(str(src), str(dest / f"{prefix}.md"))
                else:
                    src.unlink()  # .ready signals consumed
                moved_anything = True

    # Move handoff files to done/{id}/handoff/
    handoff_p = project_dir / ".agentic" / "handoff"
    if handoff_p.is_dir():
        # Day-3 (dashboard review): also catch the legacy naming agents used —
        # '{role}-{todo}-final.md'. Two exact globs on purpose: a single
        # '*-{todo}*.md' would swallow TODO-00010 when archiving TODO-0001.
        candidates = set(handoff_p.glob(f"*-{todo_id}.md")) | set(
            handoff_p.glob(f"*-{todo_id}-*.md")
        )
        for hf in sorted(candidates):
            handoff_dest = dest / "handoff"
            handoff_dest.mkdir(exist_ok=True)
            shutil.move(str(hf), str(handoff_dest / hf.name))
            moved_anything = True

    if not moved_anything:
        # P2: clean up empty dest dir (use rmtree for safety — rmdir fails
        # on non-empty, which can happen if race creates files mid-archive)
        shutil.rmtree(dest, ignore_errors=True)
        return None

    return dest


def is_archived(project_dir: str | Path, todo_id: str) -> bool:
    """DF6-3: Check if a TODO has been archived to done/{todo_id}/."""
    from . import paths

    return (paths.done_dir(project_dir) / todo_id).is_dir()
