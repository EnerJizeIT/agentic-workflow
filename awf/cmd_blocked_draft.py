"""``awf blocked-draft TODO-NNNN`` — ready REVIEW-fix text from a BLOCKED note.

Thin CLI wrapper around :func:`awf.api.blocked_draft`. Prints the draft to
stdout (or writes it to ``--out``); the TODO is never touched.

``--out`` is a user-chosen surface, but not a way around the queue: a path
inside the project's ``.agentic`` (symlinks resolved) is refused, because
that tree belongs to awf's own state (a draft dropped on a live TODO would
overwrite it). Outside ``.agentic`` the caller picks the file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def _refuse_agentic_out(project_dir: Path, out_path: Path) -> str | None:
    """Refusal text when ``out_path`` resolves inside ``.agentic``.

    ``out_path`` is already user-expanded (``~`` handled once by the
    caller), so the barrier and the actual write judge the same file.
    """
    target = out_path.resolve()
    agentic = (project_dir / ".agentic").resolve()
    try:
        target.relative_to(agentic)
    except ValueError:
        return None
    return (
        f"refusing --out {str(out_path)!r}: write to a file outside the "
        "project's .agentic, or omit --out"
    )


def run(args: Any) -> int:
    """Execute ``awf blocked-draft`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    todo_id = str(getattr(args, "todo_id", "") or "")
    out = str(getattr(args, "out", "") or "")
    out_path = Path(out).expanduser() if out else None
    if out_path is not None:
        refusal = _refuse_agentic_out(project_dir, out_path)
        if refusal is not None:
            print(f"ERROR: {refusal}")
            return 1
    try:
        result = api.blocked_draft(project_dir, todo_id)
    except api.AwfApiError as e:
        print(f"ERROR: {e}")
        return 1
    if out_path is not None:
        try:
            out_path.write_text(result.review_text, encoding="utf-8")
        except OSError as e:
            print(f"ERROR: cannot write --out {out!r}: {e}")
            return 1
        print(f"Review draft written: {out_path}")
    else:
        print(result.review_text, end="")
    return 0
