"""A32/A34: the refusal's own decisions — stay in the unit, bounded replan.

One module for the pure parts of a refusal: whether a policy may grow the
queue (``rejection_stays_in_unit``), which stage owns the finding
(``reject_fix_target``), what the replan prompt may carry and how big it
may get, and the evidence writers around those facts.

The engine keeps the transition handlers (``_handle_rejected`` /
``_maybe_rewind_rejection``) and ``_handle_fix_round`` with its rollback
budget: those are the A7 round machinery and moving them would split one
contract across two modules. Everything here is a call the handlers make.

Why a separate file at all: ``pipeline_engine`` and ``supervisor`` are
already past the 400-line ratchet, and this unit's new logic has one owner
(the refusal), not two homes (the engine loop and the supervisor stage).
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from .pipeline import Stage


def rejection_stays_in_unit(stage: Stage) -> bool:
    """A32: a rejection rewinds the SAME unit unless the policy says replan.

    ``replan`` is the only policy value that sanctions a new unit — and it
    is the only one whose path spawns the replan subprocess.
    """
    return (stage.on_rejected or "escalate") != "replan"


def reject_fix_target(stages: list[Stage], stage_idx: int) -> str:
    """A32: the stage a rejection rewinds to when the policy names none.

    The nearest earlier stage that runs a role (``kind == "execute"``) —
    the author the reviewer's finding belongs to. A rejection on the first
    stage has no earlier author, so the caller falls back to escalation.
    """
    for idx in range(stage_idx - 1, -1, -1):
        if stages[idx].kind == "execute":
            return stages[idx].name
    return ""


def is_retired_unit(project_dir: Path, todo_id: str) -> bool:
    """A32/A34: True when ``done/<id>/`` carries a RETIRED note.

    A retired unit was taken out of the queue on purpose — its id must not
    come back as a "new" clone in a replan answer.
    """
    if not todo_id:
        return False
    done = paths.done_dir(project_dir) / todo_id
    try:
        return any(done.glob("RETIRED-*.md"))
    except OSError:
        return False


def append_progress_note(project_dir: Path, todo_id: str, note: str) -> None:
    """A34: one line into ``PROGRESS-<id>.md`` (evidence, not a task).

    The progress parser counts only ``## Task`` lines, so a plain note
    never changes the unit's task counters.
    """
    from datetime import datetime, timezone

    if not todo_id:
        return
    progress = paths.outbox(project_dir) / f"PROGRESS-{todo_id}.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## replan — {stamp}\n\n{note}\n")
    except OSError:
        pass




__all__ = [
    "append_progress_note",
    "is_retired_unit",
    "reject_fix_target",
    "rejection_stays_in_unit",
]
