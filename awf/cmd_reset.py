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
    preview = getattr(args, "preview", False)
    confirm_digest = getattr(args, "confirm_digest", None) or None
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # F-R2: preview mode prints the plan (scope + revision + digest) and
    # exits without touching anything.
    if preview and not orphans:
        plan = api.preview_destruction(
            project_dir, "reset", tasks_only=tasks_only, full=full
        )
        print(api.format_preview(plan))
        return 0

    # --orphans: explicit two-step protocol (list → confirm → remove)
    if orphans:
        try:
            orphan_ids = api.list_orphans(project_dir)
        except api.AwfApiError as e:
            print(str(e))
            return 1

        if preview:
            plan = api.preview_destruction(
                project_dir, "remove-orphans", orphan_ids=orphan_ids
            )
            print(api.format_preview(plan))
            return 0

        if not orphan_ids:
            print("No orphans found.")
            return 0

        print(f"Found {len(orphan_ids)} orphan TODO(s): {', '.join(orphan_ids)}")
        if not force and not confirm_digest:
            ans = input(
                f"Remove these {len(orphan_ids)} orphan TODO(s)? [y/N] "
            ).strip().lower()
            if ans not in ("y", "yes"):
                print("Aborted.")
                return 0

        # F-DS: the decision to destroy goes through execute_destruction so
        # the source (this CLI adapter) lands in the project journal.
        plan = api.preview_destruction(
            project_dir, "remove-orphans", orphan_ids=orphan_ids
        )
        confirm = confirm_digest or plan.digest
        try:
            result = api.execute_destruction(
                project_dir,
                plan,
                confirm=confirm,
                decided_by="cli:reset --orphans",
            )
        except api.AwfApiError as e:
            print(str(e))
            return 1

        print(
            f"Removed {len(result.orphan_ids)} orphan TODO(s): "
            f"{', '.join(result.orphan_ids)}"
        )
        return 0

    # F-DS (требование 6): go through the destructive adapter so the source
    # of the decision is recorded. A missing --confirm-digest still executes
    # (the CLI invocation is itself the human decision) with the freshly
    # computed plan digest; a stale --confirm-digest is refused there.
    plan = api.preview_destruction(
        project_dir, "reset", tasks_only=tasks_only, full=full
    )
    confirm = confirm_digest or plan.digest
    decided_by = "cli:reset" + (":--confirm-digest" if confirm_digest else "")
    try:
        result = api.execute_destruction(
            project_dir, plan, confirm=confirm, decided_by=decided_by
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
