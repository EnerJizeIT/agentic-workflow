"""U5: ``awf verify-pack`` — one deterministic verify report.

Thin CLI wrapper around :func:`awf.verify_pack.verify_pack`. The exit code
follows the verdict: 0 = all measured checks ok, 1 = failures found,
2 = nothing was measured (no baseline, no contract, no gates).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf verify-pack`` and return the verdict's exit code."""
    todo_id = args.todo_id
    project_dir = Path(args.project_dir)

    try:
        result = api.verify_pack(project_dir=project_dir, todo_id=todo_id)
    except api.AwfApiError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if getattr(args, "json", False):
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"verify-pack {todo_id}")
        print(f"verdict: {result.verdict} (exit {result.exit_code}) — "
              f"{result.measured} section(s) measured")
        for name, status in result.sections.items():
            detail = result.details.get(name, "")
            suffix = f" — {detail}" if detail else ""
            print(f"  {name}: {status}{suffix}")
        if result.report_path:
            print(f"report: {result.report_path}")
    return result.exit_code
