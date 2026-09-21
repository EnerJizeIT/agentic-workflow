"""AUD05-05: advisory lock for read-modify-write state sections.

``run.yaml`` and ``current.yaml`` are both read → merge → written back.
Without mutual exclusion two concurrent writers lose each other's updates
(last write wins). This lock serializes the RMW sections so the merged
result always contains every writer's fields.

The lock lives in a SEPARATE file (``.agentic/state/.lock``): locking the
data file itself would fight with the temp+rename writes — rename replaces
the inode, and a lock held on the old inode protects nothing.

fcntl.flock on POSIX; msvcrt.locking on Windows (kept for Windows support).
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from . import paths


@contextlib.contextmanager
def locked(project_dir: Path):
    """Hold an exclusive advisory lock for one state RMW section."""
    state_dir = paths.agentic_dir(project_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_file = state_dir / ".lock"

    if sys.platform == "win32":
        import msvcrt

        f = open(lock_file, "a+b")
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            with contextlib.suppress(OSError):
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            f.close()
    else:
        import fcntl

        f = open(lock_file, "a+b")
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(f, fcntl.LOCK_UN)
            f.close()


__all__ = ["locked"]
