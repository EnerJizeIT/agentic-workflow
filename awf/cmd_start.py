"""Entry point for ``awf start`` and ``awf continue``.

Thin CLI wrapper around :func:`awf.api.start_pipeline` /
:func:`awf.api.continue_pipeline`. No legacy background path — api is the
single source of truth for both foreground and background modes.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf start`` or ``awf continue``."""
    command = getattr(args, "command", "start")
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # AUD07-02: both start and continue accept --background. The API default
    # for continue is background=True (designed for the MCP agent), so the CLI
    # passes the flag explicitly: default False = foreground for a human in
    # the terminal (the old code computed `background` here and then dropped
    # it on the continue branch — a dead variable).
    background = getattr(args, "background", False)

    if command == "continue":
        # Day-2 B3: no early "No active TODO found" pre-check here — the api
        # resolves active/blocked/pending-ACK cases and returns a message.
        header = "=== Agentic Workflow: Continuing Pipeline ==="
        ack = getattr(args, "ack", "") or ""
        if ack:
            header = f"=== Agentic Workflow: Continuing Pipeline (ACK {ack}) ==="
        print(header)

    try:
        if command == "continue":
            result = api.continue_pipeline(
                project_dir=project_dir,
                pipeline=getattr(args, "pipeline", None),
                from_stage=getattr(args, "from_stage", None),
                auto=getattr(args, "auto", False),
                timeout=getattr(args, "timeout", 3600),
                background=background,
                ack=ack,
            )
        else:
            result = api.start_pipeline(
                project_dir=project_dir,
                background=background,
                pipeline=getattr(args, "pipeline", None),
                from_stage=getattr(args, "from_stage", None),
                auto=getattr(args, "auto", False),
                timeout=getattr(args, "timeout", 3600),
                todo_id=getattr(args, "todo", "") or "",
            )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    # Background — print launch info, return immediately
    if result.run_mode == "background":
        print(result.message)
        if result.log_file:
            print(f"  log:     {result.log_file}")
            print(f"  monitor: awf status   |   tail -f {result.log_file}")
        return 0

    if result.run_mode == "noop":
        print(result.message)
        # AUD07-01: the API encodes the verdict in exit_code (e.g. 1 for the
        # foreground+checkpoint incompatibility or a user-caused refusal like
        # a garbage --ack). The CLI used to swallow it and always return 0,
        # so `awf start && next_step` sailed through a refusal.
        return result.exit_code if result.exit_code else 0

    # Foreground completed — exit code from orchestrator
    if result.exit_code is not None:
        if result.exit_code != 0:
            print(result.message, file=sys.stderr)
        return result.exit_code
    return 0
