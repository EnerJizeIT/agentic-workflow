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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .. import config as cfg_mod
from .. import paths
from .._log import log as _log
from ..infra import managed_fs
from ._errors import AwfApiError
from ._helpers import read_log_tail

# How long the launcher waits before probing a fresh background child
# (_verify_child_alive in api/pipeline.py). The reaper must not collect the
# child before that probe looks at it — see _reap_in_background.
CHILD_PROBE_SECONDS = 1.0


def _reap_in_background(
    proc: subprocess.Popen,
    logs_dir: Path,
    *,
    grace_seconds: float = CHILD_PROBE_SECONDS + 1.0,
) -> threading.Thread | None:
    """TODO-0241: reap the launched engine in a daemon thread.

    The MCP server (parent) used to never ``wait()`` on the detached child;
    a finished engine stayed a ``Zs`` zombie and its rc was lost. The
    thread calls ``proc.wait()`` (the only reaping call), logs the rc and
    exits. A Popen stand-in without ``wait`` degrades to a no-op.
    """
    if not hasattr(proc, "wait"):
        return None

    def _wait() -> None:
        # Grace: the launcher's immediate-death probe (_verify_child_alive)
        # samples the child with ``kill 0``, and a REAPED pid cannot be
        # probed. Collecting it sooner turns a just-started child into
        # "gone" and reports a spurious launch failure; the zombie must not
        # outlive the probe, not race it. The wait is tied to the probe
        # delay constant, not to a second guess.
        if grace_seconds > 0:
            time.sleep(grace_seconds)
        try:
            rc = proc.wait()
        except Exception:  # noqa: BLE001 — reaping must never crash the parent
            return
        try:
            _log(logs_dir, f"background engine exited: rc={rc} pid={proc.pid}")
        except Exception:  # noqa: BLE001
            pass

    thread = threading.Thread(
        target=_wait, name="awf-engine-reaper", daemon=True
    )
    thread.start()
    return thread


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
    # R1.1/S9-01,S9-03 (REVIEW fix F2): background launch writes both
    # ``awf-start.out`` and ``awf-start.pid`` under ``.agentic/logs``. A
    # symlinked ``.agentic`` (or ``logs``) used to send both OUTSIDE the
    # project — the same class as the worker fallback log (S9-03) and
    # ``reset`` (S9-01). Refuse the escape BEFORE creating the directory,
    # opening the log or writing the PID file; the log goes through the
    # managed no-follow helper (born 0600, fstat type check).
    # One shared derivation with the other managed writers
    # (``agentic_dir`` resolves the project, so the layout is canonical).
    root = managed_fs.agentic_root_for(logs_dir)
    log_file = logs_dir / "awf-start.out"
    pid_file = logs_dir / "awf-start.pid"
    # R1.5/F2: the logs directory is created INSIDE the no-follow walk —
    # ``open_append_log(create=True)`` below (``mkdirat`` on the verified
    # parent). A plain ``Path.mkdir`` after this preflight let a
    # ``.agentic`` swapped to a symlink between the check and the mkdir
    # create the directory OUTSIDE the project (F-B class). The preflight
    # stays as a fail-fast with a clear message before the process spawn.
    try:
        managed_fs.assert_no_symlink_escape(log_file, root=root)
        managed_fs.assert_no_symlink_escape(pid_file, root=root)
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"background launch refused — unsafe runtime path: {exc}"
        ) from exc

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
    # R1.4 (F-R1): hand the child the ExecutionId of the launch that owns
    # the project right now. The launcher published it (lease acquire)
    # before spawning; the child verifies it at start (pipeline.py) and
    # exits without work if a newer launch has since overwritten the
    # marker (a crash-orphan of an earlier launch must not run).
    from . import _lease

    exec_id = _lease.read_execution_id(project_dir)
    if exec_id:
        child_env[_lease.EXECUTION_ENV] = exec_id

    # AUD14-06c: awf-start.out carries stage crash tracebacks (which embed
    # the worker prompt) — born 0600, not 0644 via open("ab").
    # R1.1/F2: managed no-follow open (O_NOFOLLOW on the leaf, fstat type
    # check, symlinked intermediate refused) instead of a plain os.open.
    try:
        log_fd, log_file = managed_fs.open_append_log(
            log_file, root=root, create=True
        )
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"background launch refused — unsafe log path: {exc}"
        ) from exc
    except OSError as exc:
        raise AwfApiError(
            f"cannot create logs directory {logs_dir}: {exc}"
        ) from exc
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

    # TODO-0241: reap the child so a finished engine is not left as Zs and
    # its rc lands in the log. The owner starts HERE, at the spawn, not at
    # the happy return: a refusal in the PID-file write below still leaves
    # a live child, and an unowned child would never report its rc.
    _reap_in_background(proc, logs_dir)

    # R1.1/F2: the PID file goes through the same managed no-follow writer
    # as every other report/log (F-A class). A preflight re-check plus a
    # plain ``atomic_write_text`` would still follow a symlinked parent
    # swapped in after the check; ``managed_write_text`` refuses at write
    # time, so nothing lands outside the project.
    try:
        managed_fs.managed_write_text(pid_file, f"{proc.pid}\n", root=root)
    except managed_fs.ManagedPathError as exc:
        raise AwfApiError(
            f"background launch refused — unsafe PID path: {exc}"
        ) from exc
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
