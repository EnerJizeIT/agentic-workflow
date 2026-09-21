"""Lightweight file-based logger for awf.

Centralized so any module can log without circular imports.
"""
from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

# AUD15-04: default rotation threshold for orchestrator.log.
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

# path → (config mtime, resolved max bytes); config is YAML, not re-read
# on every log line.
_MAX_BYTES_CACHE: dict[str, tuple[float, int]] = {}


def _log_max_bytes(log_file: Path) -> int:
    """``automation.log_max_bytes`` from .agentic/config.yaml, or the default."""
    cfg = log_file.parent.parent / "config.yaml"
    try:
        cfg_mtime = cfg.stat().st_mtime if cfg.exists() else 0.0
    except OSError:
        cfg_mtime = 0.0
    hit = _MAX_BYTES_CACHE.get(str(log_file))
    if hit is not None and hit[0] == cfg_mtime:
        return hit[1]
    max_bytes = DEFAULT_LOG_MAX_BYTES
    try:
        import yaml

        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) if cfg.exists() else None
        value = (data or {}).get("automation", {}).get("log_max_bytes")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            max_bytes = int(value)
    except Exception:
        pass
    _MAX_BYTES_CACHE[str(log_file)] = (cfg_mtime, max_bytes)
    return max_bytes


def _maybe_rotate(log_file: Path) -> None:
    """AUD15-04: keep orchestrator.log bounded — one archived copy.

    When the live log reaches ``log_max_bytes`` it is renamed to
    ``orchestrator.log.1`` (an older archive is dropped) and the next write
    creates a fresh file. Safe with the per-line O_APPEND opens: a rotation
    racing a writer only moves the OLD file — the writer's next open
    targets the new file. Readers (awf/_log_reader.py) invalidate their
    offset cache on inode change and cold-pass archive + current.
    """
    for _ in range(3):
        try:
            size = log_file.stat().st_size
        except OSError:
            return
        if size < _log_max_bytes(log_file):
            return
        archive = log_file.parent / (log_file.name + ".1")
        try:
            if archive.exists():
                archive.unlink()
            log_file.rename(archive)
            return
        except OSError:
            continue  # a concurrent writer rotated meanwhile — re-check


def log(logs_dir: Path, message: str) -> None:
    """Append a timestamped message to ``logs_dir/orchestrator.log``."""
    if not logs_dir.exists():
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
    log_file = logs_dir / "orchestrator.log"
    _maybe_rotate(log_file)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        # AUD14-06d: O_CREAT mode 0o600 — the log is born user-only
        # instead of the old 0644 window (open("a") + later chmod).
        fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except OSError:
        pass
