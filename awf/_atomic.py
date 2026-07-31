"""Atomic file write helpers for awf-core.

Centralized so all modules use the same temp+rename pattern.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Write text file atomically via temp + rename.

    Creates parent dir if missing. Uses tempfile in same dir to guarantee
    same-filesystem rename (atomic on POSIX).

    H5 fix: plan_progress.py was direct write_text — crash mid-write
    corrupted plan.md (the only persistent progress artifact).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
