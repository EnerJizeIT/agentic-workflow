"""Entry point for ``awf start`` and ``awf continue``.

Thin CLI wrapper around :func:`awf.api.start_pipeline` /
:func:`awf.api.continue_pipeline`.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from . import api, paths, todos


def _find_active_todo(project_dir: Path) -> str:
    """Find the newest active TODO (delegates to todos.newest_active)."""
    return todos.newest_active(project_dir)


def _run_in_background(args: Any) -> int:
    """Re-launch `awf start` detached via setsid-equivalent, log to a file.

    Kept as cmd_start helper (not in api.py) because it rebuilds sys.argv —
    api.start_pipeline(background=True) uses its own argv reconstruction.
    This legacy path is preserved for full backward compat with existing
    e2e tests that pass --background via CLI.
    """
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "awf-start.out"

    # H6 fix: handle both --project-dir=X and --project-dir X forms
    raw_argv = sys.argv[1:]
    child_argv = [sys.executable, "-m", "awf", "start"]
    skip_next_value = False
    has_project_dir = False
    for i, arg in enumerate(raw_argv):
        if skip_next_value:
            skip_next_value = False
            child_argv.append(arg)
            continue
        if i == 0 and arg == "start":
            continue
        if arg == "--background":
            continue
        if arg == "--project-dir":
            has_project_dir = True
            skip_next_value = True
        elif arg.startswith("--project-dir="):
            has_project_dir = True
        child_argv.append(arg)

    if not has_project_dir:
        child_argv += ["--project-dir", str(project_dir)]

    with open(log_file, "wb") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    print(f"awf start running in background (PID {proc.pid})")
    print(f"  log:     {log_file}")
    print(f"  monitor: awf status   |   tail -f {log_file}")
    return 0


def run(args: Any) -> int:
    """Execute ``awf start`` or ``awf continue``."""
    command = getattr(args, "command", "start")
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # --background: legacy e2e-tested path, kept as-is
    if command == "start" and getattr(args, "background", False):
        return _run_in_background(args)

    if command == "continue":
        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print("No active TODO found.")
            return 0
        print("=== Agentic Workflow: Continuing Pipeline ===")
        print(f"Resuming with: {current_todo}")

    try:
        if command == "continue":
            result = api.continue_pipeline(
                project_dir=project_dir,
                pipeline=getattr(args, "pipeline", None),
                from_stage=getattr(args, "from_stage", None),
                auto=getattr(args, "auto", False),
                timeout=getattr(args, "timeout", 3600),
            )
        else:
            result = api.start_pipeline(
                project_dir=project_dir,
                pipeline=getattr(args, "pipeline", None),
                from_stage=getattr(args, "from_stage", None),
                auto=getattr(args, "auto", False),
                timeout=getattr(args, "timeout", 3600),
            )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if result.run_mode == "noop":
        print(result.message)
        return 0

    # Foreground run completed — exit code from orchestrator
    if result.exit_code is not None:
        return result.exit_code
    return 0
