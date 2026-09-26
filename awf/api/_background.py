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

from .. import config as cfg_mod
from .. import paths
from .._atomic import atomic_write_text
from ._errors import AwfApiError
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
    # RUN3 #6: single-launch checkpoint bypass — the orchestrator turns it
    # into AWF_NO_CHECKPOINTS for the process duration (see run_pipeline).
    no_checkpoints: bool = False


def start_in_background(
    project_dir: Path,
    *,
    pipeline: str | None,
    from_stage: str | None,
    auto: bool,
    timeout: int,
    todo_id: str = "",
    no_checkpoints: bool = False,
) -> tuple[int, Path, Path]:
    """Launch ``awf start`` detached, return ``(pid, log_file, pid_file)``.

    Caller wraps the returned values into :class:`awf.api.StartResult`.
    Writes PID file atomically so :func:`check_pipeline_running` can detect
    the subprocess later.

    ``no_checkpoints`` (RUN3 #6): the single-launch checkpoint bypass is
    passed to the child as ``AWF_NO_CHECKPOINTS=1`` in its environment —
    process-scoped by construction, it dies with the child and cannot leak
    into the next launch.

    ``automation.runner_dir`` (TODO-0077): when set in the project's
    config.yaml, the child's cwd is that directory instead of
    ``project_dir`` — the engine imports the pinned checkout rather than
    the project tree (awf editing itself: ``python -m awf`` puts cwd first
    on sys.path). The value is validated before spawn; an invalid value
    raises :class:`AwfApiError` with no process, no log file, no PID file.
    """
    # TODO-0077: engine checkout pin — resolved and validated BEFORE
    # anything is created (no process, no log file, no PID file on error).
    child_cwd = str(project_dir)
    runner_dir = cfg_mod.get(cfg_mod.load(project_dir), "automation.runner_dir")
    if runner_dir is not None:
        if not isinstance(runner_dir, str) or not runner_dir:
            raise AwfApiError(
                "automation.runner_dir must be a non-empty path string to a "
                f"directory containing awf/__init__.py, got {runner_dir!r}"
            )
        runner_path = Path(runner_dir).expanduser()
        if not runner_path.is_absolute():
            runner_path = Path(project_dir) / runner_path
        if not runner_path.is_dir():
            raise AwfApiError(
                "automation.runner_dir does not exist or is not a "
                f"directory: {runner_path}"
            )
        if not (runner_path / "awf" / "__init__.py").is_file():
            raise AwfApiError(
                "automation.runner_dir is not an awf checkout "
                f"(missing awf/__init__.py): {runner_path}"
            )
        child_cwd = str(runner_path)

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

    # RUN3 #6: single-launch checkpoint bypass — env for the child's
    # duration only (never in argv: the child is a plain `awf start`).
    # FU-06: the parent's own AWF_NO_CHECKPOINTS is NOT transport — it must
    # not ride along. A parent that carries the flag (a session inside a
    # no_checkpoints run) would otherwise silently disable the checkpoint
    # gate of EVERY later background launch it spawns, and the gate's log
    # would blame the launch ("start no_checkpoints=true") that never
    # passed the flag. The contract (plan_checkpoint.launch_no_checkpoints):
    # the flag is set for the duration of the pipeline process and dies
    # with it — the next launch is unaffected.
    child_env = {**os.environ, "AWF_BACKGROUND_CHILD": "1"}
    child_env.pop("AWF_NO_CHECKPOINTS", None)
    if no_checkpoints:
        child_env["AWF_NO_CHECKPOINTS"] = "1"

    # AUD14-06c: awf-start.out carries stage crash tracebacks (which embed
    # the worker prompt) — born 0600, not 0644 via open("ab").
    log_fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(log_fd, "ab") as out:
        proc = subprocess.Popen(
            child_argv,
            cwd=child_cwd,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            # DF6-5: signal child that it's a background process.
            # orchestrator's foreground+checkpoint check reads this env
            # to allow checkpoint (form opens in browser, stdout goes to file).
            env=child_env,
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
