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
    mode = "hard"
    if getattr(args, "soft", False):
        mode = "soft"
    if getattr(args, "dry_run", False):
        mode = "dry-run"
    project_dir = Path(getattr(args, "project_dir", "."))

    # AUD07-05: no local AwfApiError catch — the dispatcher's unified
    # handler (cli._run_command) prints `ERROR: <msg>` to stderr with rc 1.
    # A local `print(str(e))` to stdout used to shadow it (AUD12-11).
    result = api.rollback(
        project_dir=project_dir,
        todo_id=todo_id,
        mode=mode,
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
