"""Lightweight file-based logger for awf.

Centralized so any module can log without circular imports.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path


def log(logs_dir: Path, message: str) -> None:
    """Append a timestamped message to ``logs_dir/orchestrator.log``."""
    if not logs_dir.exists():
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
    log_file = logs_dir / "orchestrator.log"
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # AUD14-06d: O_CREAT mode 0o600 — the log is born user-only
        # instead of the old 0644 window (open("a") + later chmod).
        fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass
