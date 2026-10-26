"""Forget a unit's unkeyed history — one home for every re-run surface.

``CLOSER-<id>.json`` (first verdict, frozen wave, attempt counter) and
``CHECKLIST-<id>.json`` (the gate's verdict cache) are keyed only by the
TODO id. A later unit that reuses the id must start clean, or it inherits
``first_verdict``/``attempts`` from an attempt that no longer exists.

Three paths bring a unit id back into play: the service-run rollback, the
main ``awf rollback``, and ``restore_todo``. Each used its own list (the
first one only), which is how the main paths ended up leaving the two
files behind. They all call :func:`forget_unit_history` now.

Best effort by design: these run after the destructive step already
succeeded (git reset, archive move, refused service start), so a failed
cleanup must not turn the operation into an error.
"""
from __future__ import annotations

from pathlib import Path

from . import paths

# The unit history files — keyed by the TODO id alone, never by content.
UNIT_HISTORY_FILES = ("CLOSER-{todo_id}.json", "CHECKLIST-{todo_id}.json")


def unit_history_paths(project_dir: str | Path, todo_id: str) -> list[Path]:
    """Paths of the unit-history artifacts for *todo_id*."""
    ctx = paths.context_dir(Path(project_dir))
    return [ctx / name.format(todo_id=todo_id) for name in UNIT_HISTORY_FILES]


def forget_unit_history(project_dir: str | Path, todo_id: str) -> list[str]:
    """Remove the unit's history files; return the names actually removed."""
    removed: list[str] = []
    for path in unit_history_paths(project_dir, todo_id):
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            # Best effort: missing or unremovable is the same to the caller.
            pass
    return removed
