"""A42: the verify stage's narration — facts come from the stage builder.

IMP2b: the prompt itself is built by :mod:`awf.stage_context` (the
``supervisor`` family: ``GATES``, ``DIFF``, ``DONE`` and the role), so this
module no longer assembles text or owns a budget. What stays here is the
one narration line written after the verify subprocess ends: prompt size,
duration and the shell calls it printed.
"""
from __future__ import annotations

import re
from pathlib import Path

from ._log import log as _log

# A green pack should reach a verdict within this window (A42 target). The
# timing line names the prompt size when it does not.
VERIFY_STAGE_TARGET_SECONDS = 180.0


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def count_logged_commands(log_path: str | Path | None, start_offset: int = 0) -> int:
    """Bash invocations the verify subprocess printed after ``start_offset``.

    ``opencode run`` echoes every shell tool call as a ``$ <cmd>`` line in
    the worker log. The count is best-effort narration for the timing line:
    an unreadable/absent log or a bad offset is ``-1`` ("unknown"), never an
    exception inside the stage.
    """
    if not log_path:
        return -1
    try:
        raw = Path(log_path).read_bytes()
    except OSError:
        return -1
    if start_offset < 0 or start_offset > len(raw):
        return -1
    text = _ANSI_RE.sub("", raw[start_offset:].decode("utf-8", errors="replace"))
    return sum(1 for line in text.splitlines() if line.startswith("$ "))


def format_verify_timing(
    todo_id: str,
    *,
    prompt_chars: int,
    duration: float,
    commands: int,
    target: float | None = None,
) -> str:
    """A42: the one narration line for a verify stage.

    Shape: ``A42: verify <id>: prompt <n> chars, duration <x>s, commands
    <k>``. Over the target window the line appends the cause it can name —
    the prompt size. IMP2b dropped the attachment count: the prompt is one
    text, so a file count could only be a constant.
    """
    window = VERIFY_STAGE_TARGET_SECONDS if target is None else float(target)
    command_text = "unknown" if commands < 0 else str(commands)
    line = (
        f"A42: verify {todo_id}: prompt {prompt_chars} chars, "
        f"duration {duration:.1f}s, commands {command_text}"
    )
    if duration > window:
        line += (
            f" — over {window:.0f}s target; cause: prompt {prompt_chars} chars"
        )
    return line


def log_verify_timing(logs_dir: Path | None, line: str) -> None:
    """Write the timing line to ``orchestrator.log`` (best-effort)."""
    if logs_dir is not None:
        _log(logs_dir, line)


__all__ = [
    "VERIFY_STAGE_TARGET_SECONDS",
    "count_logged_commands",
    "format_verify_timing",
    "log_verify_timing",
]
