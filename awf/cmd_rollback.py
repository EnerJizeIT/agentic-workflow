"""Port of lib/rollback.sh — ``awf rollback`` command.

Thin CLI wrapper around :func:`awf.api.rollback`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf rollback`` and return exit code."""
    todo_id = args.todo_id
    soft = getattr(args, "soft", False)
    preview = getattr(args, "preview", False)
    confirm_digest = getattr(args, "confirm_digest", None) or None
    project_dir = Path(getattr(args, "project_dir", "."))

    # F-R2/F-A2: preview prints the plan (baseline + mode + digest) and exits.
    # Terminal UNCONDITIONALLY, like init/reset — --dry-run must not shadow it
    # (the old branch required mode in ("hard","soft"), so ``--preview
    # --dry-run`` silently fell through to a plain dry-run / ERROR).
    if preview:
        # F-3 (аудит 0218): план обязан отражать ФАКТИЧЕСКИЙ режим — тот же,
        # что исполнил бы запуск без --preview. ``--dry-run`` переводит
        # выполнение в mode="dry-run"; раньше preview всё равно печатал
        # ``rollback(hard)``.
        plan_mode = "dry-run" if getattr(args, "dry_run", False) else (
            "soft" if soft else "hard"
        )
        plan = api.preview_destruction(
            project_dir, "rollback", mode=plan_mode, todo_id=todo_id,
        )
        print(api.format_preview(plan))
        return 0

    mode = "hard"
    if soft:
        mode = "soft"
    if getattr(args, "dry_run", False):
        mode = "dry-run"

    # F-DS (требование 6): route through the destructive adapter so the CLI
    # adapter records the source of the decision. A missing --confirm-digest
    # still executes with the freshly computed plan digest (dry-run records
    # nothing — it destroys nothing); a stale digest is refused there.
    plan = api.preview_destruction(
        project_dir, "rollback", mode=mode, todo_id=todo_id
    )
    confirm = confirm_digest or plan.digest
    decided_by = "cli:rollback" + (":--confirm-digest" if confirm_digest else "")
    # AUD07-05: no local AwfApiError catch — the dispatcher's unified
    # handler (cli._run_command) prints `ERROR: <msg>` to stderr with rc 1.
    # A local `print(str(e))` to stdout used to shadow it (AUD12-11).
    result = api.execute_destruction(
        project_dir, plan, confirm=confirm, decided_by=decided_by
    )

    print(f"Rolling back {result.todo_id} to baseline: {result.baseline_sha}")
    print(f"Mode: {result.mode}")

    if result.mode == "dry-run":
        print()
        print("Changes since baseline:")
        print(result.diff_stat)
        return 0

    if result.diff_stat.strip():
        print()
        print("Changes reset:")
        print(result.diff_stat)

    print()
    print(f"Rolled back to {result.baseline_sha}")
    if result.ack_file:
        print(f"ACK file: {result.ack_file}")
    return 0
