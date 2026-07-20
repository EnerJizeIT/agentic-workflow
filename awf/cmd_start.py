"""Entry point for ``awf start`` and ``awf continue``."""
from __future__ import annotations

import subprocess
import sys
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


def _run_in_background(args: Any) -> int:
    """Re-launch `awf start` detached via setsid-equivalent, log to a file.

    This is the Python equivalent of the old bash `setsid awf start ... </dev/null >LOG 2>&1 &`.
    `start_new_session=True` does the setsid; stdin/out/err are redirected so the
    detached process is fully independent of the launching terminal.
    """
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "awf-start.out"

    # Rebuild the argv WITHOUT --background so the detached child runs normally.
    # We keep all other args (including --auto, --pipeline, --from-stage, --timeout).
    child_argv = [sys.executable, "-m", "awf", "start"]
    for arg in sys.argv[1:]:
        # Skip the literal program name (bin/awf or ./bin/awf) and --background.
        if arg in ("start", "--background"):
            continue
        child_argv.append(arg)

    # Make sure --project-dir points to the resolved path (so the child runs in
    # the right place even if the caller later cd's elsewhere).
    if "--project-dir" not in child_argv:
        child_argv += ["--project-dir", str(project_dir)]

    with open(log_file, "wb") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # equivalent of setsid — detached process group
        )

    print(f"awf start running in background (PID {proc.pid})")
    print(f"  log:     {log_file}")
    print(f"  monitor: awf status   |   tail -f {log_file}")
    return 0


def run(args: Any) -> int:
    """Execute ``awf start`` or ``awf continue``."""
    command = getattr(args, "command", "start")
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # --background: re-exec detached, then return immediately.
    if command == "start" and getattr(args, "background", False):
        return _run_in_background(args)

    if command == "continue":
        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print("No active TODO found.")
            return 0
        print("=== Agentic Workflow: Continuing Pipeline ===")
        print(f"Resuming with: {current_todo}")

    return run_pipeline(args)
