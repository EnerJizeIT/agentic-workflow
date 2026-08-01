"""Port of lib/baseline.sh — ``awf baseline`` command.

Thin CLI wrapper around :func:`awf.api.create_baseline`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf baseline`` and return exit code."""
    todo_id = args.todo_id
    project_dir = Path(getattr(args, "project_dir", "."))

    print(f"Creating baseline for {todo_id}...")

    try:
        result = api.create_baseline(project_dir=project_dir, todo_id=todo_id)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    print(f"  SHA: {result.sha}")
    if result.test_status == "passed":
        print("  Test baseline: passed")
    elif result.test_status == "failed":
        print("  Test baseline: failed (recorded as-is)")
    elif result.test_status == "no_test_cmd":
        print("  Test baseline: no test_cmd configured")
    elif result.test_status == "no_config":
        print("  Test baseline: no config.yaml")

    print()
    print("Baseline files created:")
    for fname in result.files_created:
        print(f"  {fname}")
    return 0
