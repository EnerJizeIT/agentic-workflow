"""``awf todo-update TODO-NNNN --content-file <path> | --content "…"`` —
reword a not-started TODO, keeping the number (RUN6 #4).

The number, the dispatch ``.ready`` and the baseline stay; the previous
content is backed up to ``context/TODO-<id>.md.bak-<ts>``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf todo-update``."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")
    reason = str(getattr(args, "reason", "") or "")
    content_file = str(getattr(args, "content_file", "") or "")

    content = str(getattr(args, "content", "") or "")
    if content_file:
        try:
            content = Path(content_file).resolve().read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot read --content-file {content_file!r}: {e}")
            return 1

    print("=== Agentic Workflow: Update TODO ===")
    try:
        result = api.update_todo(project_dir, todo_id, content, reason=reason)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    print(result.message)
    return 0
