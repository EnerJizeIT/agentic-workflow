"""``awf dispatch`` — create a unit atomically (TODO + baseline + .ready).

Thin CLI wrapper around :func:`awf.api.dispatch_todo`, the same entry point
the MCP ``awf_dispatch_todo`` uses. Content comes from ``--content`` or
``--content-file``; ``--source-ref`` inserts the source-of-truth section.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf dispatch`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    content = str(getattr(args, "content", "") or "")
    content_file = str(getattr(args, "content_file", "") or "")
    if content_file:
        try:
            content = Path(content_file).resolve().read_text(encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot read --content-file {content_file!r}: {e}")
            return 1

    source_ref = str(getattr(args, "source_ref", "") or "") or None
    try:
        result = api.dispatch_todo(
            project_dir,
            content,
            role=getattr(args, "role", None) or None,
            todo_id=getattr(args, "todo_id", None) or None,
            pipeline=getattr(args, "pipeline", None) or None,
            source_ref=source_ref,
        )
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1

    print(f"Dispatched {result.todo_id} (baseline {result.baseline_sha})")
    for path in result.files_written:
        print(f"  {path}")
    for warning in result.pre_check_warnings:
        print(f"  warning: {warning}")
    return 0
