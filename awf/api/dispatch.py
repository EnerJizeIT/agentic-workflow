"""TODO dispatch: atomic create + baseline + ready signal.

Replaces the 3-step manual workflow:
  1. Write inbox/TODO-NNNN.md
  2. awf_baseline TODO-NNNN (snapshot)
  3. touch inbox/TODO-NNNN.ready (signal)

With one call. Auto-picks next NNNN, creates baseline snapshot, dispatches
the signal. Supervisor gets back the todo_id + baseline_sha to track.
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import paths
from .._atomic import atomic_write_text
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import DispatchTodoResult
from .pipeline import create_baseline


def _next_todo_id(project_dir: Path) -> str:
    """Pick next TODO-NNNN id (max existing + 1).

    Scans both inbox and outbox for any TODO-NNNN.* files to avoid
    collisions with already-completed TODOs.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    max_num = 0
    for d in (inbox, outbox):
        if not d.is_dir():
            continue
        for f in d.glob("TODO-*.md"):
            m = re.match(r"^TODO-(\d+)$", f.stem)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num

    return f"TODO-{max_num + 1:04d}"


def dispatch_todo(
    project_dir: Path,
    content: str,
    *,
    role: str | None = None,
    todo_id: str | None = None,
) -> DispatchTodoResult:
    """Atomically create a TODO, baseline it, dispatch the signal.

    Three-step manual workflow compressed into one call:
      1. Write ``.agentic/inbox/TODO-NNNN.md`` (atomic).
      2. Create ``.agentic/context/BASELINE-NNNN.{sha,status,tests.log,env.log}``.
      3. Write ``.agentic/inbox/TODO-NNNN.ready`` signal (trigger for plan stage).

    Args:
        project_dir: awf project root (must contain .agentic/).
        content: TODO markdown body (the task description).
        role: optional role hint for logging/debugging (does not affect
            pipeline routing — that's determined by stage order in
            pipeline.yaml). Useful for supervisor context: "this TODO
            is for agent-system-analyst".
        todo_id: override auto-generated id (e.g. "TODO-0007"). If None,
            auto-picks next available NNNN by scanning inbox + outbox.

    Returns:
        DispatchTodoResult with todo_id, baseline_sha, files written.

    Raises:
        AwfApiError: if .agentic/ missing or content empty.
    """
    if not content or not content.strip():
        raise AwfApiError("content is required (non-empty TODO body)")

    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    if todo_id is None:
        todo_id = _next_todo_id(project_dir)
    elif not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}' — expected format 'TODO-NNNN' (4+ digits)"
        )

    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)

    # Optional role hint as HTML comment (invisible to LLM reading the .md,
    # visible to grep / debugging).
    body = content
    if role:
        body = f"<!-- role_hint: {role} -->\n" + body

    # Step 1: write TODO-NNNN.md (atomic — crash-safe)
    md_path = inbox / f"{todo_id}.md"
    if md_path.exists():
        raise AwfApiError(
            f"{md_path.name} already exists in inbox. Use a different todo_id "
            "or remove the existing file first."
        )
    atomic_write_text(md_path, body)

    # Step 2: create baseline snapshot (sha + tests.log + env.log + status)
    baseline = create_baseline(project_dir, todo_id)

    # Step 3: dispatch signal
    ready_path = inbox / f"{todo_id}.ready"
    ready_path.touch()

    return DispatchTodoResult(
        todo_id=todo_id,
        baseline_sha=baseline.sha,
        role_hint=role,
        files_written=[
            f".agentic/inbox/{todo_id}.md",
            f".agentic/inbox/{todo_id}.ready",
            *baseline.files_created,
        ],
    )


__all__ = ["dispatch_todo", "_next_todo_id"]
