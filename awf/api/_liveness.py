"""AUD04-07: единый резолвер живости пайплайна.

До: живость определяли два независимых кода, и они расходились:

1. ``state.pipeline_pid`` (current.yaml) → ``api/pipeline.py::_is_pipeline_running``
   — читали start/continue-гарды и ``kill_pipeline``;
2. ``.agentic/logs/awf-start.pid`` → ``api/_background.py::check_pipeline_running``
   — читали ``awf status``.

Разная строгость PID-проверки → status говорил «не работает», а start-гард
«уже работает» (и наоборот). Foreground-пайплайн (без PID-файла) status
не видел вовсе.

Теперь один код-путь (:func:`resolve`) смотрит ОБА источника и применяет
одну строгую идентификацию: PID считается нашим только если argv процесса
``... -m awf start|continue ...`` (чтение ``/proc/<pid>/cmdline`` на Linux).
Без /proc (не-Linux) — деградация до проверки живости (как раньше).
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from .. import paths
from ..pipeline_state import read_state

_OUR_SUBCOMMANDS = ("start", "continue")
PID_FILE_NAME = "awf-start.pid"


def read_cmdline(pid: int) -> str | None:
    """Raw ``/proc/<pid>/cmdline`` (NUL-separated); None if dead/unreadable.

    Seam for tests (PID-reuse scenarios) — patch this, not os internals.
    """
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return None


def is_our_argv(argv: Sequence[str]) -> bool:
    """argv looks like ``... -m awf start|continue ...``."""
    if "-m" in argv:
        i = argv.index("-m")
        if i + 2 < len(argv) and argv[i + 1] == "awf" and argv[i + 2] in _OUR_SUBCOMMANDS:
            return True
    return False


def cmdline_is_ours(cmdline: str) -> bool:
    """Identity from a raw cmdline string (NUL-separated or plain text)."""
    argv = [a for a in cmdline.replace("\x00", " ").split() if a]
    return is_our_argv(argv)


def probe_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` liveness probe.

    OverflowError = pid outside the OS range (test fakes like 2**31) —
    such a process cannot be alive.
    """
    try:
        os.kill(pid, 0)
    except (OSError, OverflowError):
        return False
    return True


def pid_is_ours(pid: int, cmdline: str | None, *, strict: bool = True) -> bool:
    """Liveness + identity for one candidate PID.

    strict=True (Linux): cmdline unreadable → NOT ours (never assume,
    QA .14 — a reused PID must not pass as our pipeline).
    strict=False (non-Linux degradation): cmdline unreadable → liveness
    alone, identity cannot be checked without /proc.
    """
    if not probe_alive(pid):
        return False
    if cmdline is None:
        return not strict
    return cmdline_is_ours(cmdline)


def _proc_available() -> bool:
    return Path("/proc/self").is_dir()


def _parse_pid_file(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def resolve(project_dir: Path) -> tuple[bool, int | None, str | None]:
    """(running, pid, source) — the single liveness code path.

    Source 1: ``.agentic/logs/awf-start.pid`` (background launcher).
    Source 2: ``state.pipeline_pid`` (any launcher — the orchestrator
    writes it at every stage, so a FOREGROUND pipeline without a PID
    file is visible to ``awf status``).

    A stale PID file (dead or foreign process) is cleaned up here.
    """
    pid_file = paths.agentic_dir(project_dir) / "logs" / PID_FILE_NAME
    if pid_file.is_file():
        pid = _parse_pid_file(pid_file)
        if pid is not None and pid_is_ours(
            pid, read_cmdline(pid), strict=_proc_available()
        ):
            return True, pid, "pid_file"
        # dead or foreign → clean up the stale file
        try:
            pid_file.unlink()
        except OSError:
            pass

    state = read_state(project_dir)
    if state:
        try:
            spid = int(state.get("pipeline_pid"))
        except (TypeError, ValueError):
            spid = None
        if spid and pid_is_ours(spid, read_cmdline(spid), strict=True):
            return True, spid, "state"

    return False, None, None


__all__ = [
    "read_cmdline",
    "is_our_argv",
    "cmdline_is_ours",
    "probe_alive",
    "pid_is_ours",
    "resolve",
]
