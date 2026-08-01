"""Background subprocess management for long-running pipelines.

Two responsibilities:

1. :func:`start_in_background` — launch ``awf start`` detached, write
   PID file for status polling, return immediately with run_id + log_file.
2. :func:`check_pipeline_running` — probe PID file liveness, return
   log tail. Called by :func:`awf.api.get_status` for MCP-4 polling.

PID file location: ``.agentic/logs/awf-start.pid`` (atomic write).
Stale files (process exited) are cleaned automatically.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import paths
from .._atomic import atomic_write_text
from ._helpers import read_log_tail


@dataclass
class PipelineArgs:
    """Minimal args namespace for :func:`awf.orchestrator.run_pipeline`.

    The orchestrator reads these via ``getattr(args, name)``. No
    ``command`` field — orchestrator doesn't use it (start vs continue
    dispatching happens at the api layer above).
    """

    project_dir: str = "."
    pipeline: str | None = None
    from_stage: str | None = None
    auto: bool = False
    timeout: int = 3600


def start_in_background(
    project_dir: Path,
    *,
    pipeline: str | None,
    from_stage: str | None,
    auto: bool,
    timeout: int,
) -> tuple[int, Path, Path]:
    """Launch ``awf start`` detached, return ``(pid, log_file, pid_file)``.

    Caller wraps the returned values into a :class:`awf.api.StartResult`.
    Writes PID file atomically so :func:`check_pipeline_running` can detect
    the subprocess later.
    """
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = logs_dir / "awf-start.out"
    pid_file = logs_dir / "awf-start.pid"

    child_argv: list[str] = [
        sys.executable,
        "-m",
        "awf",
        "start",
        "--project-dir",
        str(project_dir),
    ]
    if pipeline:
        child_argv += ["--pipeline", pipeline]
    if from_stage:
        child_argv += ["--from-stage", from_stage]
    if auto:
        child_argv.append("--auto")
    child_argv += ["--timeout", str(timeout)]

    with open(log_file, "wb") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    atomic_write_text(pid_file, f"{proc.pid}\n")
    return proc.pid, log_file, pid_file


def check_pipeline_running(
    project_dir: Path,
    *,
    log_tail_lines: int = 20,
) -> tuple[bool, int | None, str | None]:
    """Detect a running awf pipeline by reading PID file + os.kill probe.

    Returns ``(is_running, pid, log_tail)``:
    - ``is_running``: True if PID file exists AND process is alive.
    - ``pid``: PID from file, or None.
    - ``log_tail``: last N lines of ``.agentic/logs/awf-start.out``
      (None if file absent).

    Stale PID files (process exited) are cleaned up automatically.
    """
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    pid_file = logs_dir / "awf-start.pid"
    log_file = logs_dir / "awf-start.out"

    if not pid_file.is_file():
        return False, None, read_log_tail(log_file, log_tail_lines)

    try:
        pid_str = pid_file.read_text(encoding="utf-8").strip()
        pid = int(pid_str)
    except (OSError, ValueError):
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False, None, read_log_tail(log_file, log_tail_lines)

    try:
        os.kill(pid, 0)
        alive = True
    except (OSError, ProcessLookupError):
        alive = False

    if not alive:
        try:
            pid_file.unlink()
        except OSError:
            pass
        return False, None, read_log_tail(log_file, log_tail_lines)

    return True, pid, read_log_tail(log_file, log_tail_lines)


__all__ = [
    "PipelineArgs",
    "start_in_background",
    "check_pipeline_running",
]
