"""``awf review-append TODO-NNNN --text "…" | --text-file <path>`` —
append a supervisor REVIEW-fix section to a started TODO (TODO-0240).

The section is numbered automatically (``виток N``), the previous content
is backed up to ``context/TODO-<id>.md.bak-<ts>``. A live pipeline on the
unit must be stopped first; an archived unit is refused.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf review-append``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")
    reason = str(getattr(args, "reason", "") or "")
    text_file = str(getattr(args, "text_file", "") or "")

    text = str(getattr(args, "text", "") or "")
    if text_file:
        try:
            text = Path(text_file).resolve().read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot read --text-file {text_file!r}: {e}")
            return 1

    operation_id = str(getattr(args, "operation_id", "") or "") or None
    print("=== Agentic Workflow: Review Append ===")
    try:
        result = api.review_append(
            project_dir,
            todo_id,
            text,
            reason=reason,
            operation_id=operation_id,
        )
    except api.AwfApiError as e:
        # R1.2 (TODO-0216, F3), same contract as ``awf todo-update``: an
        # unknown durable-operation outcome is NOT a plain refusal to retry.
        # Print the machine token and return a distinct exit code so a
        # script can tell them apart (TODO-0240 review: this CLI copied the
        # sibling without this branch).
        outcome = getattr(e, "outcome", "")
        if outcome:
            print(f"outcome={outcome}: {e}")
            return 2
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
