"""Lightweight file-based logger for awf.

Centralized so any module can log without circular imports.
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
from pathlib import Path

# AUD15-04: default rotation threshold for orchestrator.log.
DEFAULT_LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB

# FU-17b2 (TODO-0070): rotated archives are kept, not dropped — this many.
DEFAULT_LOG_ARCHIVE_KEEP = 3

# path → (config mtime, resolved max bytes); config is YAML, not re-read
# on every log line.
_MAX_BYTES_CACHE: dict[str, tuple[float, int]] = {}

# FU-17b2: serializes the rename step so two threads of this process
# never rotate the same live file at once. The lock does NOT make archive
# names unique (two now() calls can land on the same microsecond and the
# threads share the pid) — _maybe_rotate guards against that with an
# exists-check: a rename onto an existing archive would replace it.
_ROTATE_LOCK = threading.Lock()


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


def archive_files(log_file: Path) -> list[Path]:
    """FU-17b2: rotated archives of ``log_file``, oldest first.

    An archive is any regular sibling named ``<log_file.name>.*`` — the
    live file itself has no trailing dot, so it never matches. Ordered by
    mtime with the name as tie-break: the name embeds a microsecond
    stamp, so the order stays correct even on filesystems with coarse
    mtime resolution.
    """
    prefix = log_file.name + "."
    try:
        entries = list(log_file.parent.iterdir())
    except OSError:
        return []
    found: list[tuple[float, str, Path]] = []
    for p in entries:
        if not p.name.startswith(prefix):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file():
            found.append((st.st_mtime, p.name, p))
    found.sort()
    return [p for _, _, p in found]


def _prune_archives(log_file: Path, keep: int | None = None) -> None:
    """FU-17b2: drop the oldest rotated archives beyond the keep limit.

    Only ever deletes files the rotation itself creates
    (``<name>.*`` siblings) — the live log and foreign files are
    untouched. A concurrent prune is harmless: unlink on a missing file
    is a no-op.
    """
    if keep is None:
        keep = DEFAULT_LOG_ARCHIVE_KEEP
    archives = archive_files(log_file)
    for old in archives[: max(0, len(archives) - keep)]:
        try:
            old.unlink()
        except OSError:
            pass


def _maybe_rotate(log_file: Path) -> None:
    """AUD15-04 / FU-17b2: keep orchestrator.log bounded — K archives.

    When the live log reaches ``log_max_bytes`` it is renamed to
    ``<name>.<UTC-microsecond-stamp>-<pid>`` — unique per rotation, so
    two concurrent rotators each keep their own archive instead of
    unlinking each other's (the old unlink + rename-onto-``.1`` pair lost
    the first rotation's lines). After the rename, the oldest archives
    beyond :data:`DEFAULT_LOG_ARCHIVE_KEEP` are pruned.

    Safe with the per-line O_APPEND opens: a rotation racing a writer
    only moves the OLD file — the writer's next open targets the new
    file. Readers (awf/_log_reader.py) invalidate their offset cache on
    inode change and cold-pass all archives (oldest first) + current.
    """
    for _ in range(3):
        try:
            size = log_file.stat().st_size
        except OSError:
            return
        if size < _log_max_bytes(log_file):
            return
        with _ROTATE_LOCK:
            stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive = log_file.parent / f"{log_file.name}.{stamp}-{os.getpid()}"
            if archive.exists():
                # Stamp collision (same microsecond, same pid): renaming
                # onto the existing archive would replace it and lose its
                # lines — the loss this scheme exists to prevent. Re-stamp
                # once; if the clock did not advance, skip this rotation
                # (the live file stays oversized, the next log line
                # retries) instead of overwriting.
                stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                archive = log_file.parent / f"{log_file.name}.{stamp}-{os.getpid()}"
                if archive.exists():
                    return
            try:
                log_file.rename(archive)
            except OSError:
                continue  # a concurrent rotator took the file — re-check
        _prune_archives(log_file)
        return


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
