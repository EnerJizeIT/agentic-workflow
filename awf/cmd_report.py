"""Port of lib/report.sh — ``awf report`` command.

Thin CLI wrapper around :func:`awf.api.get_report`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf report`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", "."))

    try:
        result = api.get_report(project_dir=project_dir)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    print("==============================================")
    print("  Agentic Workflow Report")
    print(f"  Project: {result.project_name}")
    print(f"  Generated: {result.generated_at}")
    print("==============================================")
    print()

    for item in result.items:
        status = item["status"]
        todo_id = item["todo_id"]
        if status == "OK":
            print(f"  OK   {todo_id}")
        elif status == "BLK":
            print(f"  BLK  {todo_id}")
        else:
            print(f"  ...  {todo_id}  (in progress)")

    print()
    print(f"Completed: {result.done_count} | Blocked: {result.blocked_count}")

    if result.git_diff.strip():
        print()
        print("Files changed:")
        print(result.git_diff)
    else:
        print()
        print("Files changed: (no changes or not a git repo)")

    if result.latest_test_log_tail:
        print()
        print("Latest test results:")
        print(result.latest_test_log_tail)

    return 0
