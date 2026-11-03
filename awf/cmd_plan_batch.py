"""Entry point for ``awf plan-batch <id>...`` (A25).

Same behaviour as the MCP tool ``awf_plan_batch``: inbox drafts (no
``.ready``) plus the ordered plan. stdout carries the plan as JSON — the
exact ``plan`` list the tool returns — so a script can diff the two.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import api


def run(args: Any) -> int:
    """Draft the batch; 1 with a clean message on any refusal."""
    items = [
        str(item).strip() for item in (getattr(args, "items", None) or []) if str(item).strip()
    ]
    if not items:
        print("plan-batch: at least one backlog id is required", file=sys.stderr)
        return 1
    try:
        result = api.plan_batch(
            getattr(args, "project_dir", "."),
            items,
            getattr(args, "goal", "") or "",
            backlog_path=(getattr(args, "backlog", "") or None),
        )
    except api.AwfApiError as e:
        print(f"plan-batch: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result.plan, ensure_ascii=False, indent=2))
    return 0
