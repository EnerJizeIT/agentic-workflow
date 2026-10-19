"""R1.5 (roadmap R1): the Store-owned writers of the state-file projections.

Architecture §2 makes the Store the SINGLE writer of project state. The
legacy ``run.yaml`` / ``current.yaml`` / ``service-run.yaml`` files stay on
disk as read-only projections for the legacy readers during R1, but their
BYTES leave the process only through the two functions here. Every module
that used to write a state file directly (``run_state``, ``pipeline_state``,
``lifecycle``) now calls into this package instead, so there is exactly one
locus outside which a state-file write is a bug. The architectural guard
``tests/negative/test_roadmap_r1_no_bypass.py`` enforces that locus.

Why a separate module and not ``_io``: ``_io`` owns the CONTROL SNAPSHOT
primitive (``control.json``); these two own the LEGACY PROJECTIONS. Keeping
the distinction lets the guard name the projections explicitly while the
snapshot keeps its own durable writer.
"""
from __future__ import annotations

from pathlib import Path

from .. import managed_fs


def write_state_file(path: Path, content: str) -> None:
    """Publish ``content`` to a state-file projection, atomically.

    ``path`` MUST be a control-state file (``run.yaml`` / ``current.yaml`` /
    ``service-run.yaml``) already resolved by its path owner
    (``run_state.run_file`` / ``pipeline_state`` path).

    R1 carry (roadmap R2.5): the write goes through the shared managed
    writer, so the barrier matches the log/dispatch writers — a symlinked
    ``.agentic`` / ``state`` component is refused before a single byte
    lands, instead of the old ``atomic_write_text`` following the link out
    of the project. The temp-file + rename mechanics are unchanged. The
    walk root is the ONE shared derivation
    (``managed_fs.agentic_root_for``), not a second anchor next to it.
    """
    managed_fs.managed_write_text(
        path, content, root=managed_fs.agentic_root_for(path.parent)
    )


def remove_state_file(path: Path) -> None:
    """Remove a state-file projection if present (idempotent).

    R2.5 (architect-review): the destructive sibling of the write carries
    the SAME no-follow barrier. Before this the write refused a symlinked
    ``.agentic`` / ``state`` while ``remove`` still followed the link and
    deleted a file OUTSIDE the project — the asymmetry the R1 carry
    introduced. ``assert_no_symlink_escape`` is the shared preflight for
    destructive ops (its own docstring), the same one the inbox/outbox
    cleaners run before their first unlink.
    """
    managed_fs.assert_no_symlink_escape(
        path, root=managed_fs.agentic_root_for(path.parent)
    )
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = ["write_state_file", "remove_state_file"]
