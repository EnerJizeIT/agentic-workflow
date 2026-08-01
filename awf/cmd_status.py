"""Port of lib/status.sh — ``awf status`` command.

Thin CLI wrapper around :func:`awf.api.get_status`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf status`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    try:
        result = api.get_status(project_dir=project_dir)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    print("=== Agentic Workflow Status ===")
    print(f"Project: {result.project_name}")
    print()

    # MCP-4: pipeline subprocess state (background awf start)
    if result.pipeline_running:
        print(f"Pipeline: RUNNING (PID {result.pipeline_pid})")
        if result.log_tail:
            print("--- last log lines ---")
            print(result.log_tail)
            print("---")
        print()

    for entry in result.active_todos:
        todo_id = entry["todo_id"]
        print(f"Active: {todo_id}")

        ack = entry.get("ack")
        if ack is not None:
            print(f"  ACK: {ack}")

        progress = entry.get("progress")
        if progress:
            print(f"  Progress: {progress['done']}/{progress['total']} tasks done")
            if progress["failed"] > 0:
                print(f"  Failed:  {progress['failed']} task(s)")
            if progress["last"]:
                print(f"  Last:    {progress['last']}")
        else:
            print("  Progress: (none — worker has not started)")

    for bid in result.blocked_ids:
        print(f"Blocked: {bid}")

    print()
    print("Summary:")
    print(f"  Completed: {result.done_count}")
    print(f"  Blocked:   {result.blocked_count}")
    print(f"  Active:    {len(result.active_todos)}")

    if result.conflict_warning:
        print()
        print(f"\u26a0\ufe0f  {result.conflict_warning}")

    if result.suggestion:
        print()
        print(result.suggestion)

    return 0
