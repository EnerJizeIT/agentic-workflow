"""Internal helpers for awf.api: precondition checks + safe file reads.

All functions here are private (``_`` prefix). Public API functions in
:mod:`awf.api.lifecycle`, :mod:`awf.api.pipeline`, :mod:`awf.api.roles`
use them as building blocks.
"""
from __future__ import annotations

from pathlib import Path

from .. import git_utils, paths
from ._errors import AwfApiError


def require_agentic(project_dir: Path) -> Path:
    """Ensure .agentic/ exists. Raise AwfApiError if not.

    Returns the resolved .agentic/ Path on success.
    """
    agentic = paths.agentic_dir(project_dir)
    if not agentic.is_dir():
        raise AwfApiError(
            f"No .agentic/ found at {project_dir}. Run 'awf init' first."
        )
    return agentic


def require_git_repo(project_dir: Path) -> None:
    """Ensure project is a git repo. Raise AwfApiError if not."""
    if not git_utils.is_git_repo(project_dir):
        raise AwfApiError(
            f"Not a git repository: {project_dir}. Run 'git init' first."
        )


def read_file_text(path: Path, max_chars: int | None = None) -> str:
    """Read file as text, optionally truncated to ``max_chars``.

    Returns a placeholder string on OSError (never raises) — used by
    init_project which prefers degraded content over total failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return f"(error reading {path.name}: {e})"
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + "\n...[truncated]"
    return text


def read_log_tail(log_file: Path, n: int) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent."""
    if not log_file.is_file():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return ""
    tail = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail)


__all__ = [
    "require_agentic",
    "require_git_repo",
    "read_file_text",
    "read_log_tail",
]
