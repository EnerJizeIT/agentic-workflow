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
import os
import sys
from pathlib import Path

from . import paths
from .infra import managed_fs

#: Name of the RMW lock file inside ``.agentic/state/``. Owned here so the
#: force-init cleanup (``lifecycle._clean_state_dir``) preserves the exact
#: file this module locks instead of re-spelling the name next to it.
LOCK_NAME = ".lock"


@contextlib.contextmanager
def locked(project_dir: Path):
    """Hold an exclusive advisory lock for one state RMW section."""
    state_dir = paths.agentic_dir(project_dir) / "state"
    lock_file = state_dir / LOCK_NAME

    # F-4 (audit 0218): ``sys.platform == "win32"`` is the ONE predicate for
    # "the msvcrt platform". The previous ``not managed_fs.is_posix() and
    # sys.platform == "win32"`` was logically identical (``is_posix()`` is
    # ``os.name == "posix"``) — the extra conjunct changed no branch, only
    # the reader's idea of it. The real fix for the exotic-platform leak is
    # the ``try/except ImportError`` around ``import fcntl`` below.
    if sys.platform == "win32":
        import msvcrt

        state_dir.mkdir(parents=True, exist_ok=True)
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
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover — exotic platform
            raise RuntimeError(
                "advisory state locking needs fcntl (POSIX) or msvcrt "
                f"(Windows); platform {sys.platform!r} is unsupported — "
                "refusing instead of running without a lock"
            ) from exc

        # R1.1/F-A: the lock writer lives under ``.agentic/state`` — a
        # symlinked ``.agentic``/``state`` used to send the lock file (and
        # its ``mkdir``) outside the project. Open through the managed
        # no-follow helper (symlink → refusal). Reset still must not delete
        # the lock — that stays reset's responsibility, not the writer's.
        root = Path(project_dir).resolve()
        fd = managed_fs.open_managed(
            lock_file, flags=os.O_RDWR | os.O_CREAT, root=root
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


__all__ = ["LOCK_NAME", "locked"]
