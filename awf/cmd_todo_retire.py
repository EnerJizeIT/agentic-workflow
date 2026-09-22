"""``awf todo-retire TODO-NNNN --reason "…"`` — retire a rejected/abandoned TODO.

RUN5 #2: a rejected TODO (DONE-{id}.{md,json} in outbox without the
``DONE-{id}.ready`` signal) stays "active" forever — ``todo-retire`` moves
it to ``done/<id>/`` with a RETIRED note and no fake closure signal.
``awf restore`` still brings it back.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf todo-retire``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")
    reason = str(getattr(args, "reason", "") or "")

    print("=== Agentic Workflow: Retire TODO ===")
    try:
        result = api.retire_todo(project_dir, todo_id, reason)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
