"""R5: ``awf context-pack`` — build the stage context pack.

Thin CLI wrapper around :mod:`awf.context_pack`. Without ``--stdout`` the
pack is written to ``.agentic/context/STAGE-<todo>-<stage>.md`` and the exit
code is 0; with ``--stdout`` the same bytes go to stdout and no file is
created or changed. An unknown TODO or stage is exit code 2 with the value
in the message, and no file is written (AC18–AC20).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import api
from ._atomic import atomic_write_text


def run(args: Any) -> int:
    """Execute ``awf context-pack`` and return its exit code."""
    stage = getattr(args, "stage", None) or getattr(args, "stage_pos", None)
    todo_id = args.todo_id
    project_dir = Path(args.project_dir)

    if not stage:
        print("ERROR: unknown stage: --stage is required", file=sys.stderr)
        return 2

    try:
        if getattr(args, "stdout", False):
            print(api.render_context_pack(project_dir, todo_id, stage), end="")
            return 0
        if getattr(args, "out", None):
            # ``--out`` names its own file: the K2 path stays untouched.
            target = Path(args.out)
            atomic_write_text(target, api.render_context_pack(project_dir, todo_id, stage))
        else:
            # ONE writer for the K2 path — the builder, not a copy of it.
            target = api.build_context_pack(project_dir, todo_id, stage).path
    except api.AwfApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(target)
    return 0
