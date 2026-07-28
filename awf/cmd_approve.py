"""Entry point for ``awf approve <todo-id>``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import paths


def run(args: Any) -> int:
    """Create APPROVE-TODO-NNNN.ready signal to authorize auto-commit."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = getattr(args, "todo_id", "")

    if not todo_id:
        print("Usage: awf approve <TODO-ID>")
        return 1

    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    signal = inbox / f"APPROVE-{todo_id}.ready"
    signal.touch()
    print(f"Approved {todo_id}. Pipeline (if waiting) will commit and continue.")
    return 0
