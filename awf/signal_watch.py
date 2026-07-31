"""BD-20: subprocess execution with signal-file watch + grace-period termination.

Extracted from orchestrator.py (A6 refactor) — keeps signal-watch logic
isolated from the pipeline state machine.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from ._env import awf_subprocess_env
from ._log import log as _log

BD20_POLL_INTERVAL = 3
BD20_HARD_TIMEOUT = 3600
BD20_SIGNAL_GRACE_SECONDS = 10


def run_subprocess_until_signal(
    cmd: list[str],
    cwd: str | Path,
    watch_paths: list[Path] | None = None,
    watch_new_glob: tuple[Path, str] | None = None,
    logs_dir: Path | None = None,
    hard_timeout: int = BD20_HARD_TIMEOUT,
    grace_seconds: int = BD20_SIGNAL_GRACE_SECONDS,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """BD-20: run subprocess, watch for signal files, terminate if it lingers.

    Polls the subprocess every ``BD20_POLL_INTERVAL`` seconds. As soon as
    ANY of these conditions fires, gives ``grace_seconds`` to exit, then
    SIGTERM (and SIGKILL after 5s if still alive):

    - any path in ``watch_paths`` exists AFTER subprocess start (BD-22 fix:
      snapshot-based — stale files that existed before subprocess launch
      are ignored; only NEW appearances count as signal)
    - any NEW file matching ``watch_new_glob`` appears in the snapshot
      taken at start (used when the filename is picked by the subprocess
      itself, e.g. ``TODO-*.ready`` for supervisor create_todo)

    Args:
        cmd: command list.
        cwd: working directory.
        watch_paths: concrete paths whose NEW existence means "work is done".
        watch_new_glob: (directory, glob_pattern) — detect NEW files matching
            the pattern that didn't exist at subprocess start.
        logs_dir: optional, for logging.
        hard_timeout: absolute cap.
        grace_seconds: grace period after signal detected.
        env: subprocess environment (defaults to os.environ).
    """
    import time

    watch_paths = watch_paths or []
    # BD-22: snapshot which watch_paths already exist at start (stale signals
    # from previous runs). Only paths that DON'T exist at start, or that
    # appear AFTER start, count as a valid signal.
    pre_existing: set[str] = {str(p) for p in watch_paths if p.exists()}
    if pre_existing and logs_dir:


        _log(
            logs_dir,
            f"BD-22: ignoring {len(pre_existing)} stale watch_paths "
            f"(exist before subprocess start)",
        )

    snapshot: set[str] = set()
    if watch_new_glob is not None:
        watch_dir, pattern = watch_new_glob
        if watch_dir.is_dir():
            snapshot = {p.name for p in watch_dir.glob(pattern)}

    proc = subprocess.Popen(cmd, cwd=str(cwd), env=env or awf_subprocess_env())
    deadline = time.monotonic() + hard_timeout
    signal_seen_at: float | None = None

    while True:
        rc = proc.poll()
        if rc is not None:
            if logs_dir:


                _log(logs_dir, f"subprocess exited naturally with code {rc}")
            return subprocess.CompletedProcess(cmd, rc)

        now = time.monotonic()

        if signal_seen_at is None:
            # BD-22: a path counts as signal only if it was NOT in pre_existing
            # snapshot (i.e., appeared DURING subprocess execution).
            triggered = any(
                str(p) not in pre_existing and p.exists() for p in watch_paths
            )
            if not triggered and watch_new_glob is not None:
                watch_dir, pattern = watch_new_glob
                if watch_dir.is_dir():
                    current = {p.name for p in watch_dir.glob(pattern)}
                    if current - snapshot:
                        triggered = True
            if triggered:
                signal_seen_at = now
                if logs_dir:


                    _log(
                        logs_dir,
                        f"BD-20: signal detected, granting {grace_seconds}s grace "
                        f"for subprocess to exit (pid={proc.pid})",
                    )

        if signal_seen_at is not None:
            if now - signal_seen_at >= grace_seconds:
                if logs_dir:
                    _log(
                        logs_dir,
                        f"BD-20: grace expired, terminating subprocess (pid={proc.pid})",
                    )
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                # H7 fix: was CompletedProcess(cmd, 0) — masked killed process
                # as success. Use real returncode (-15 for SIGTERM, -9 for SIGKILL).
                # Callers check returncode != 0 to detect abnormal termination.
                real_rc = proc.returncode if proc.returncode is not None else -15
                return subprocess.CompletedProcess(cmd, real_rc)

        if now >= deadline:
            if logs_dir:
                _log(logs_dir, f"BD-20: hard timeout reached, killing (pid={proc.pid})")
            proc.kill()
            proc.wait()
            raise TimeoutError(
                f"Subprocess did not produce signal within {hard_timeout}s"
            )

        time.sleep(BD20_POLL_INTERVAL)
