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
    todo_id: str = ""


def start_in_background(
    project_dir: Path,
    *,
    pipeline: str | None,
    from_stage: str | None,
    auto: bool,
    timeout: int,
    todo_id: str = "",
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
    if todo_id:
        child_argv += ["--todo", todo_id]
    if auto:
        child_argv.append("--auto")
    child_argv += ["--timeout", str(timeout)]

    # AUD14-06c: awf-start.out carries stage crash tracebacks (which embed
    # the worker prompt) — born 0600, not 0644 via open("ab").
    log_fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(log_fd, "ab") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=str(project_dir),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # DF6-5: signal child that it's a background process.
            # orchestrator's foreground+checkpoint check reads this env
            # to allow checkpoint (form opens in browser, stdout goes to file).
            env={**os.environ, "AWF_BACKGROUND_CHILD": "1"},
        )

    atomic_write_text(pid_file, f"{proc.pid}\n")
    return proc.pid, log_file, pid_file


def check_pipeline_running(
    project_dir: Path,
    *,
    log_tail_lines: int = 20,
) -> tuple[bool, int | None, str | None]:
    """Detect a running awf pipeline (AUD04-07: shared liveness resolver).

    One code path over BOTH sources — the background PID file AND
    ``state.pipeline_pid`` — so a foreground pipeline (no PID file) is
    visible to ``awf status``. Identity is strict: the PID is ours only
    if its argv is ``... -m awf start|continue ...`` (AUD-8 PID-reuse
    defense, hardened). Stale PID files (dead/foreign process) are
    cleaned up by the resolver.

    Returns ``(is_running, pid, log_tail)``:
    - ``is_running``: True if a live, identity-verified pipeline exists.
    - ``pid``: the pipeline PID, or None.
    - ``log_tail``: last N lines of ``.agentic/logs/awf-start.out``
      (None if file absent).
    """
    from ._liveness import resolve

    log_file = paths.agentic_dir(project_dir) / "logs" / "awf-start.out"
    running, pid, _source = resolve(project_dir)
    return running, pid, read_log_tail(log_file, log_tail_lines)


__all__ = [
    "PipelineArgs",
    "start_in_background",
    "check_pipeline_running",
]
