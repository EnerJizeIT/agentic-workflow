"""Port of lib/reset.sh — ``awf reset`` command.

Thin CLI wrapper around :func:`awf.api.reset_runtime` /
:func:`awf.api.list_orphans` / :func:`awf.api.remove_orphans`.

For ``--orphans`` mode, uses the explicit two-step protocol
(``list_orphans`` → confirm → ``remove_orphans``) to avoid computing
orphan ids twice.
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
    force = getattr(args, "force", False)
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # --orphans: explicit two-step protocol (list → confirm → remove)
    if orphans:
        try:
            orphan_ids = api.list_orphans(project_dir)
        except api.AwfApiError as e:
            print(str(e))
            return 1

        if not orphan_ids:
            print("No orphans found.")
            return 0

        print(f"Found {len(orphan_ids)} orphan TODO(s): {', '.join(orphan_ids)}")
        if not force:
            ans = input(
                f"Remove these {len(orphan_ids)} orphan TODO(s)? [y/N] "
            ).strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted.")
                return 0

        try:
            result = api.remove_orphans(project_dir, orphan_ids)
        except api.AwfApiError as e:
            print(str(e))
            return 1

        print(
            f"Removed {len(result.orphan_ids)} orphan TODO(s): "
            f"{', '.join(result.orphan_ids)}"
        )
        return 0

    # Other modes: delegate directly to reset_runtime
    try:
        result = api.reset_runtime(
            project_dir=project_dir,
            tasks_only=tasks_only,
            full=full,
            orphans=False,
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if result.mode == "noop":
        print(f"No .agentic/ found at {project_dir}.")
        return 0

    print(f"=== Reset complete (mode: {result.mode}) ===")
    if result.cleaned_dirs:
        print(f"Cleaned directories: {', '.join(result.cleaned_dirs)}")
    return 0
