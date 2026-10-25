"""``awf supervise`` — restart a dead engine within a bounded budget.

TODO-0242 (P0-2): the CLI face of :func:`awf.api.supervise_run`. Exit code
0 when the run ended or the loop was stopped by the iteration bound; 1 for
the exhausted budget and a failed restart (the operator must look).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def run(args: Any) -> int:
    from .api import supervise_run

    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    result = supervise_run(
        project_dir,
        max_restarts=int(getattr(args, "max_restarts", 5)),
        pause_seconds=float(getattr(args, "pause_seconds", 30.0)),
    )
    print(result["message"])
    print(f"  restarts: {result['restarts']}, log: {result['log_file']}")
    return 0 if result["action"] in ("stopped", "no_run") else 1
