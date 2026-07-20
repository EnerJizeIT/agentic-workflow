"""Entry point for ``awf start`` and ``awf continue``."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from . import todos
from .orchestrator import run_pipeline


def _find_active_todo(project_dir: Path) -> str:
    """Find the newest active TODO."""
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    active = todos.list_active_todos(inbox, outbox)
    return active[0] if active else ""


def run(args: Any) -> int:
    """Execute ``awf start`` or ``awf continue``."""
    command = getattr(args, "command", "start")
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    if command == "continue":
        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print("No active TODO found.")
            return 0
        print(f"=== Agentic Workflow: Continuing Pipeline ===")
        print(f"Resuming with: {current_todo}")

    return run_pipeline(args)
