"""``awf restore TODO-NNNN`` — bring an archived TODO back to the inbox.

Safety net for the reconcile incident (NEG-2026-09-19 R2a): a TODO that was
archived without work returns to the inbox, armed with a fresh .ready signal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf restore``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")

    print("=== Agentic Workflow: Restore TODO ===")
    try:
        result = api.restore_todo(project_dir, todo_id)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
