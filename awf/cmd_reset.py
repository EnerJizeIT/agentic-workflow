"""Port of lib/reset.sh — ``awf reset`` command.

Thin CLI wrapper around :func:`awf.api.reset_runtime`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf reset`` and return exit code."""
    tasks_only = getattr(args, "tasks_only", False)
    full = getattr(args, "full", False)
    orphans = getattr(args, "orphans", False)
    project_dir = Path(getattr(args, "project_dir", "."))

    if orphans:
        # --orphans is destructive — keep interactive confirm when --force not set
        force = getattr(args, "force", False)
        if not force:
            from . import paths, todos
            inbox = paths.inbox(project_dir)
            outbox = paths.outbox(project_dir)
            active_ids = todos.list_active_todos(inbox, outbox)
            orphan_ids = [tid for tid in active_ids if not todos.has_progress(outbox, tid)]
            if orphan_ids:
                print(f"Found {len(orphan_ids)} orphan TODO(s): {', '.join(orphan_ids)}")
                ans = input(f"Remove these {len(orphan_ids)} orphan TODO(s)? [y/N] ").strip().lower()
                if ans not in ("y", "yes"):
                    print("Aborted.")
                    return 0

    try:
        result = api.reset_runtime(
            project_dir=project_dir,
            tasks_only=tasks_only,
            full=full,
            orphans=orphans,
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if result.mode == "noop":
        print(f"No .agentic/ found at {project_dir}.")
        return 0

    if result.mode == "orphans":
        if result.orphan_ids:
            print(f"Removed {len(result.orphan_ids)} orphan TODO(s): {', '.join(result.orphan_ids)}")
        else:
            print("No orphans found.")
        return 0

    print(f"=== Reset complete (mode: {result.mode}) ===")
    if result.cleaned_dirs:
        print(f"Cleaned directories: {', '.join(result.cleaned_dirs)}")
    return 0
