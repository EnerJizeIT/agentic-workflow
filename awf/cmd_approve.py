"""Entry point for ``awf approve <todo-id>``.

Thin CLI wrapper around :func:`awf.api.approve_commit`.
"""
from __future__ import annotations

from typing import Any

from . import api


def run(args: Any) -> int:
    """Create APPROVE-TODO-NNNN.ready signal to authorize auto-commit."""
    try:
        result = api.approve_commit(
            project_dir=getattr(args, "project_dir", "."),
            todo_id=getattr(args, "todo_id", ""),
            # AUD07-04: --evidence flag (parity with the MCP awf_approve
            # tool). Validation stays in the API — in run mode evidence is
            # required and the AwfApiError below explains why.
            evidence=getattr(args, "evidence", "") or "",
            # U11 (B5): optional tree fingerprint from verify time; the API
            # refuses when the tree moved since (see awf tree-sha).
            verified_sha=getattr(args, "verified_sha", "") or "",
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1
    print(f"Approved {result.todo_id}. Pipeline (if waiting) will commit and continue.")
    print(f"Signal: {result.signal_file}")
    if result.verified_sha_file:
        print(f"Verified tree: {result.verified_sha_file}")
    return 0
