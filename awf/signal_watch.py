"""BD-20: subprocess execution with signal-file watch + natural-exit wait.

Extracted from orchestrator.py (A6 refactor) — keeps signal-watch logic
isolated from the pipeline state machine.

Design (BD-20 redesign): detecting a signal file means "work is logically
done". We do NOT kill the subprocess — it may still be flushing buffers,
closing connections, writing metadata. We poll until natural exit. The
only safety net is ``hard_timeout`` (default 3600s) which SIGKILLs a
hung process.
"""
from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from ._env import awf_subprocess_env
from ._log import log as _log

BD20_POLL_INTERVAL = 3
BD20_HARD_TIMEOUT = 3600


def _sort_key_by_numeric_id(name: str) -> tuple[int, str]:
    """Sort signal filenames by embedded numeric ID.

    TODO-0010 should come after TODO-0009, not before TODO-0002.
    """
    m = re.search(r"(\d+)", name)
    return (int(m.group(1)) if m else 0, name)


def _build_pre_snapshot(
    watch_paths: list[Path],
    watch_new_glob: tuple[Path, str] | None,
    logs_dir: Path | None = None,
) -> tuple[set[str], set[str]]:
    """BD-22: snapshot watch_paths and glob files that exist before subprocess start.

    Returns (pre_existing_paths, glob_snapshot) — both used to detect
    only NEW appearances during subprocess execution.
    """
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

    return pre_existing, snapshot


def _detect_new_signal(
    watch_paths: list[Path],
    pre_existing: set[str],
    watch_new_glob: tuple[Path, str] | None,
    snapshot: set[str],
) -> str | None:
    """Check if a new signal file appeared since subprocess start.

    Returns the signal filename if detected, None otherwise.
    Checks concrete watch_paths first, then glob pattern.
    """
    # BD-22: a path counts as signal only if it was NOT in pre_existing
    # snapshot (i.e., appeared DURING subprocess execution).
    for p in watch_paths:
        if str(p) not in pre_existing and p.exists():
            return p.name

    if watch_new_glob is not None:
        watch_dir, pattern = watch_new_glob
        if watch_dir.is_dir():
            current = {p.name for p in watch_dir.glob(pattern)}
            new_files = current - snapshot
            if new_files:
                return sorted(new_files, key=_sort_key_by_numeric_id)[0]

    return None


def run_subprocess_until_signal(
    cmd: list[str],
    cwd: str | Path,
    watch_paths: list[Path] | None = None,
    watch_new_glob: tuple[Path, str] | None = None,
    logs_dir: Path | None = None,
    hard_timeout: int = BD20_HARD_TIMEOUT,
    env: dict[str, str] | None = None,
    signal_holder: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """BD-20: run subprocess, watch for signal files, wait for natural exit.

    Polls every ``BD20_POLL_INTERVAL`` seconds. As soon as ANY of these
    conditions fires, the subprocess is considered "logically done" — we
    keep waiting for its natural exit (no SIGTERM/SIGKILL):

    - any path in ``watch_paths`` exists AFTER subprocess start (BD-22 fix:
      snapshot-based — stale files that existed before subprocess launch
      are ignored; only NEW appearances count as signal)
    - any NEW file matching ``watch_new_glob`` appears in the snapshot
      taken at start (used when the filename is picked by the subprocess
      itself, e.g. ``TODO-*.ready`` for supervisor create_todo)

    ``hard_timeout`` is the absolute cap — if exceeded, the process is
    SIGKILLed (hung-process safety net, not normal flow).

    Args:
        cmd: command list.
        cwd: working directory.
        watch_paths: concrete paths whose NEW existence means "work is done".
        watch_new_glob: (directory, glob_pattern) — detect NEW files matching
            the pattern that didn't exist at subprocess start.
        logs_dir: optional, for logging.
        hard_timeout: absolute cap before SIGKILL (default 3600s).
        env: subprocess environment (defaults to os.environ).
        signal_holder: optional dict populated with the EXACT signal file
            name (e.g. ``"TODO-0042.ready"``) that fired. Lets callers
            avoid re-globbing the inbox and picking a stale TODO by mtime
            (auditor HIGH finding on supervisor.py:496). Backward-compatible:
            callers that don't pass it get the original behavior.
    """
    watch_paths = watch_paths or []
    # KAUD-4: handle hard_timeout=None (use default)
    if hard_timeout is None:
        hard_timeout = BD20_HARD_TIMEOUT

    pre_existing, snapshot = _build_pre_snapshot(watch_paths, watch_new_glob, logs_dir)

    from ._env import _pdeathsig_preexec
    # KAUD-9: redirect worker stdout/stderr to log file instead of inheriting.
    # Prevents mixing worker output with awf-start.out.
    worker_log = None
    if logs_dir and logs_dir.is_dir():
        # Derive a log file name from the command (role name)
        log_name = "worker-output.out"
        for arg in cmd:
            if "agent-" in str(arg) and ".md" not in str(arg):
                log_name = f"{arg}.out"
                break
        worker_log = open(logs_dir / log_name, "w", encoding="utf-8")  # noqa: SIM115
        # P2: restrict worker log permissions (may contain sensitive output)
        import os as _os
        _os.chmod(logs_dir / log_name, 0o600)
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env or awf_subprocess_env(),
            stdout=worker_log if worker_log else None,
            stderr=subprocess.STDOUT if worker_log else None,
            preexec_fn=_pdeathsig_preexec,
        )
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
                fired_name = _detect_new_signal(
                    watch_paths, pre_existing, watch_new_glob, snapshot,
                )
                if fired_name:
                    signal_seen_at = now
                    if signal_holder is not None:
                        signal_holder["signal"] = fired_name
                    if logs_dir:
                        _log(
                            logs_dir,
                            f"BD-20: signal detected ({fired_name}), waiting for "
                            f"natural exit (pid={proc.pid})",
                        )

            if now >= deadline:
                if logs_dir:
                    _log(logs_dir, f"BD-20: hard timeout reached, killing (pid={proc.pid})")
                proc.kill()
                proc.wait()
                if signal_seen_at is not None:
                    raise TimeoutError(
                        f"Signal was detected but process did not exit within {hard_timeout}s"
                    )
                raise TimeoutError(
                    f"Subprocess did not produce signal within {hard_timeout}s"
                )

            time.sleep(BD20_POLL_INTERVAL)
    finally:
        if worker_log:
            worker_log.close()
