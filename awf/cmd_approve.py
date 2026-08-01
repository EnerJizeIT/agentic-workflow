"""Entry point for ``awf approve <todo-id>``.

Thin CLI wrapper around :func:`awf.api.approve_commit`.
"""
from __future__ import annotations

from typing import Any

from . import api


def run(args: Any) -> int:
    """Create APPROVE-TODO-NNNN.ready signal to authorize auto-commit."""
    try:
        result = api.approve_commit(
            project_dir=getattr(args, "project_dir", "."),
            todo_id=getattr(args, "todo_id", ""),
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1
    print(f"Approved {result.todo_id}. Pipeline (if waiting) will commit and continue.")
    print(f"Signal: {result.signal_file}")
    return 0
