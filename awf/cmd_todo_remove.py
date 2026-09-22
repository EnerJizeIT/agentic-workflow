"""``awf todo-remove TODO-NNNN`` — delete a TODO that never started.

RUN3 #5: an inert TODO (``.md`` without ``.ready``/signals/progress) is
moved to ``done/<id>/removed-<timestamp>.md`` — the trace stays. Armed or
signalled TODOs are refused with a hint (awf unblock / awf reset --orphans).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf todo-remove``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")

    print("=== Agentic Workflow: Remove TODO ===")
    try:
        result = api.remove_todo(project_dir, todo_id)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
