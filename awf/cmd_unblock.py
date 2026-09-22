"""``awf unblock TODO-NNNN`` — clear stale BLOCKED/ACK closure signals.

RUN3 #4: a stale ``outbox/BLOCKED-<id>.ready`` keeps a re-issued TODO
invisible to ``awf_status``/``awf_start``. This command moves the closure
signals to a ``context/`` trace directory so the TODO is active again.
DONE closures are never touched (``awf restore`` only).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf unblock``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")

    print("=== Agentic Workflow: Unblock TODO ===")
    try:
        result = api.unblock_todo(project_dir, todo_id)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
