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

from ..._atomic import atomic_write_text


def write_state_file(path: Path, content: str) -> None:
    """Publish ``content`` to a state-file projection, atomically.

    ``path`` MUST be a control-state file (``run.yaml`` / ``current.yaml`` /
    ``service-run.yaml``) already resolved by its path owner
    (``run_state.run_file`` / ``pipeline_state`` path). The write mechanics
    (temp + rename) are the shared :func:`awf._atomic.atomic_write_text`;
    this wrapper exists so the LOCUS of the write is the Store package, not
    a caller of the facade.
    """
    atomic_write_text(path, content)


def remove_state_file(path: Path) -> None:
    """Remove a state-file projection if present (idempotent)."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


__all__ = ["write_state_file", "remove_state_file"]
