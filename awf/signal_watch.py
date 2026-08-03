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

import shutil
import subprocess
import time
from pathlib import Path

from ._env import awf_subprocess_env
from ._log import log as _log

BD20_POLL_INTERVAL = 3
BD20_HARD_TIMEOUT = 3600


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

    # QA-E: early check for 'opencode' binary specifically (most common case).
    # Tests use mock commands like "x" — don't break those.
    if cmd and isinstance(cmd, list) and cmd[0] == "opencode":
        if not shutil.which("opencode"):
            raise FileNotFoundError(
                "'opencode' not found on PATH. Install opencode or add to PATH."
            )

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
            fired_name: str | None = None
            for p in watch_paths:
                if str(p) not in pre_existing and p.exists():
                    triggered = True
                    fired_name = p.name
                    break
            else:
                triggered = False
                if watch_new_glob is not None:
                    watch_dir, pattern = watch_new_glob
                    if watch_dir.is_dir():
                        current = {p.name for p in watch_dir.glob(pattern)}
                        new_files = current - snapshot
                        if new_files:
                            triggered = True
                            # Pick lexicographically smallest for determinism
                            # when multiple TODO-*.ready arrive in same poll.
                            fired_name = sorted(new_files)[0]
            if triggered:
                signal_seen_at = now
                if signal_holder is not None and fired_name:
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
            raise TimeoutError(
                f"Subprocess did not produce signal within {hard_timeout}s"
            )

        time.sleep(BD20_POLL_INTERVAL)
