"""U4: ``awf prove-red`` — machine proof that tests are red on baseline.

Thin CLI wrapper around :func:`awf.prove_red.prove_red`. Exit code follows
the verdict: 0 = red-ok, 1 = not-red / green-after, 2 = broken-runner
(also used when the check cannot run at all: missing baseline, bad ids).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf prove-red`` and return the verdict's exit code."""
    todo_id = args.todo_id
    project_dir = Path(args.project_dir)
    tests = [str(t) for t in args.tests] if getattr(args, "tests", None) else None

    try:
        result = api.prove_red(project_dir=project_dir, todo_id=todo_id, tests=tests)
    except api.AwfApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"prove-red {todo_id} (baseline {result.baseline_sha[:12]})")
        print(f"verdict: {result.verdict} (exit {result.exit_code})")
        print(result.message)
        for w in result.warnings:
            print(f"warning: {w}")
    return result.exit_code
