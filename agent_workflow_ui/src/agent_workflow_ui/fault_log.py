"""MCP server fault log: lifecycle and crash traces in a file.

TODO-0181 (owner diagnosis 2026-10-09): when opencode kills the MCP
session, there is neither a server traceback nor a lifecycle mark —
"why it died" cannot be reconstructed. This module writes a small file:

- a START line (plugin version, pid, argv) at setup;
- a STOP line on normal exit (atexit);
- a CRASH / THREAD-CRASH traceback on unhandled exceptions
  (``sys.excepthook`` + ``threading.excepthook``).

Location: ``$XDG_STATE_HOME/awf/mcp-server.log`` (fallback
``~/.local/state/awf/``); override with the ``AWF_MCP_LOG`` env var
(tests). Rotation: one step to ``.1`` when the file exceeds 1 MB.

Any error in the logger itself is swallowed — the server must never
die because of its logger.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 1024 * 1024  # rotation threshold: one step to .1

_lock = threading.Lock()
_active: Path | None = None
_prev_excepthook = None


def _default_log_path() -> Path:
    """AWF_MCP_LOG, else $XDG_STATE_HOME/awf/mcp-server.log, else ~/.local/state/awf/."""
    env = os.environ.get("AWF_MCP_LOG", "").strip()
    if env:
        return Path(env).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "awf" / "mcp-server.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _version() -> str:
    try:
        from . import __version__

        return __version__
    except Exception:
        return "unknown"


def _line(tag: str, detail: str = "") -> str:
    text = f"[{_now()}] {tag}"
    if detail:
        text += f" {detail}"
    return text


def _rotate(path: Path) -> None:
    """Size rotation, one step: over the limit → move to .1 (overwrite)."""
    try:
        if path.is_file() and path.stat().st_size > MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        pass


def _write(path: Path, text: str) -> None:
    """Append one entry. Never raises (a logger must not take down the server)."""
    with _lock:
        try:
            _rotate(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(text)
                if not text.endswith("\n"):
                    fh.write("\n")
        except Exception:
            pass


def _stop_line(path: Path) -> None:
    _write(path, _line("STOP", f"pid={os.getpid()}"))


def _excepthook(exc_type, exc, tb) -> None:
    try:
        if _active is not None:
            detail = "".join(traceback.format_exception(exc_type, exc, tb))
            _write(_active, _line("CRASH", f"pid={os.getpid()}\n{detail.rstrip()}"))
    except Exception:
        pass
    hook = _prev_excepthook if _prev_excepthook is not None else sys.__excepthook__
    hook(exc_type, exc, tb)


def _thread_excepthook(exc_details) -> None:
    try:
        if _active is None:
            return
        # Python < 3.12 passes a dict, 3.12+ passes an ExceptHookArgs
        # namedtuple — accept both.
        if isinstance(exc_details, dict):
            thread = exc_details.get("thread")
            exc_type = exc_details.get("exc_type")
            exc_value = exc_details.get("exc_value")
            exc_tb = exc_details.get("exc_traceback")
        else:
            thread = exc_details.thread
            exc_type = exc_details.exc_type
            exc_value = exc_details.exc_value
            exc_tb = exc_details.exc_traceback
        name = thread.name if thread is not None else "unknown"
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _write(
            _active,
            _line("THREAD-CRASH", f"thread={name} pid={os.getpid()}\n{detail.rstrip()}"),
        )
    except Exception:
        pass


def setup_fault_log(log_path: str | os.PathLike[str] | None = None) -> Path:
    """Install the fault log; returns the log path. Never raises.

    ``log_path`` wins over the ``AWF_MCP_LOG`` env var, which wins over
    the XDG-state default. Idempotent: a repeat call returns the
    already-active path without re-registering.
    """
    global _active, _prev_excepthook
    if _active is not None:
        return _active
    path = Path(log_path).expanduser() if log_path else _default_log_path()
    _active = path
    argv = " ".join(sys.argv) or "-"
    _write(path, _line("START", f"agent-workflow-ui {_version()} pid={os.getpid()} argv={argv}"))
    atexit.register(_stop_line, path)
    if sys.excepthook is not _excepthook:
        _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    return path


def _reset() -> None:
    """Test helper: forget the active log (hooks stay installed, inert)."""
    global _active
    with _lock:
        _active = None
