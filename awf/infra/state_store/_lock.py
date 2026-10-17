"""R1.3: the interprocess project lock for control-state transactions.

Reuses the shared ``.agentic/state/.lock`` (symlink-safe via ``managed_fs``),
so state-store writers and the legacy inbox writers serialize on the same
lock. Not re-entrant: flock is per open file description, so a nested
implicit acquire would deadlock against itself — it raises instead.
"""
from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from ._errors import StateStoreError

_LOCK_LOCAL = threading.local()


@contextlib.contextmanager
def project_lock(project_dir: str | Path):
    """Hold the project lock for one transaction.

    No bypass parameter: every caller opens the lock here. A facade that
    needs two sections under one lock composes them INSIDE one ``transact``
    or calls this once around both — an "already held" escape hatch would
    let a caller skip the lock by mistake.
    """
    held = getattr(_LOCK_LOCAL, "held", set())
    key = str(Path(project_dir).resolve())
    if key in held:
        raise StateStoreError(
            "project lock is not re-entrant — a nested transact would "
            "deadlock (flock is per open file description); combine the "
            "sections under one lock instead"
        )
    from ..._lock import locked

    with locked(project_dir):
        _LOCK_LOCAL.held = set(getattr(_LOCK_LOCAL, "held", set())) | {key}
        try:
            yield
        finally:
            _LOCK_LOCAL.held = set(getattr(_LOCK_LOCAL, "held", set())) - {key}


__all__ = ["project_lock"]
