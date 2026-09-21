"""Entry point for ``awf todo-draft <AUDIT-ID|FU-NN>`` (U11, E3).

Prints the draft skeleton to stdout, or writes it to ``--out`` (an
existing file is never overwritten without ``--force``).
"""
from __future__ import annotations

import sys
from typing import Any

from . import todo_draft


def run(args: Any) -> int:
    """Generate the task-file draft; 1 with a clean message on any refusal."""
    try:
        out_path, draft = todo_draft.make_todo_draft(
            getattr(args, "query", ""),
            index_path=(getattr(args, "index", "") or None),
            project_dir=getattr(args, "project_dir", "."),
            out=(getattr(args, "out", "") or None),
            force=bool(getattr(args, "force", False)),
        )
    except todo_draft.TodoDraftError as e:
        print(f"todo-draft: {e}", file=sys.stderr)
        return 1
    if out_path is not None:
        head = "\n".join(draft.splitlines()[:10])
        print(f"Draft written: {out_path}")
        print("── skeleton head ──")
        print(head)
    else:
        print(draft)
    return 0
