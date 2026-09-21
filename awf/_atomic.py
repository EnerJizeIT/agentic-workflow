"""Atomic file write helpers for awf-core.

Centralized so all modules use the same temp+rename pattern.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(
    path: Path,
    content: str,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> None:
    """Write text file atomically via temp + rename.

    Creates parent dir if missing. Uses tempfile in same dir to guarantee
    same-filesystem rename (atomic on POSIX).

    H5 fix: plan_progress.py was direct write_text — crash mid-write
    corrupted plan.md (the only persistent progress artifact).

    AUD01-06: mkstemp tmp is 0600, so a blind os.replace used to flip an
    existing 0644 file to user-only. The final file keeps the existing
    file's mode (or ``mode`` when given); new files stay 0600.
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
        if mode is not None:
            os.chmod(tmp_path, mode)
        elif path.exists():
            os.chmod(tmp_path, path.stat().st_mode & 0o777)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
