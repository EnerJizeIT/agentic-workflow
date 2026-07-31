"""Port of lib/reset.sh — ``awf reset`` command."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import paths, todos


def run(args: Any) -> int:
    """Execute ``awf reset`` and return exit code."""
    tasks_only = getattr(args, "tasks_only", False)
    full = getattr(args, "full", False)
    orphans = getattr(args, "orphans", False)
    force = getattr(args, "force", False)
    project_dir = Path(getattr(args, "project_dir", "."))

    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        print(f"No .agentic/ found at {project_dir}.")
        return 0

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    if orphans:
        return _reset_orphans(inbox, outbox, force)

    print("=== Reset agentic workflow ===")

    dirs_to_clean = []
    if full or not tasks_only:
        dirs_to_clean = ["inbox", "outbox", "context", "logs", "reports"]
    else:
        dirs_to_clean = ["inbox", "outbox"]

    if full:
        print("Cleaning all runtime directories...")
    elif tasks_only:
        print("Cleaning inbox and outbox...")
    else:
        print("Cleaning all runtime (keeping phases)...")

    for d in dirs_to_clean:
        dirpath = agentic / d
        if dirpath.is_dir():
            for f in dirpath.iterdir():
                if f.is_file():
                    f.unlink()
                elif f.is_dir():
                    import shutil
                    shutil.rmtree(f)

    print("Done. Runtime cleaned.")
    return 0


def _reset_orphans(inbox: Path, outbox: Path, force: bool) -> int:
    """Remove orphan TODOs — active ones with no progress."""
    print("=== Looking for orphan TODOs (active, no progress) ===")

    active_ids = todos.list_active_todos(inbox, outbox)
    orphan_ids: list[str] = []

    for todo_id in active_ids:
        if not todos.has_progress(outbox, todo_id):
            orphan_ids.append(todo_id)

    if not orphan_ids:
        print("No orphans found. Every active TODO has a PROGRESS file.")
        return 0

    print(f"Found {len(orphan_ids)} orphan TODO(s):")
    for tid in orphan_ids:
        ready_file = inbox / f"{tid}.ready"
        ready_time = "(unknown)"
        if ready_file.exists():
            try:
                mtime = ready_file.stat().st_mtime
                ready_time = time.ctime(mtime)
            except OSError:
                pass
        print(f"  {tid}   created: {ready_time}")
    print()

    if not force:
        ans = input(f"Remove these {len(orphan_ids)} orphan TODO(s)? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0

    for tid in orphan_ids:
        ready = inbox / f"{tid}.ready"
        md = inbox / f"{tid}.md"
        if ready.exists():
            ready.unlink()
        if md.exists():
            md.unlink()
        print(f"  removed: {tid}")

    print("Done. Active TODOs with progress are untouched.")
    return 0
