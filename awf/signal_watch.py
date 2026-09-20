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

from . import _net, _proc
from ._env import awf_subprocess_env
from ._log import log as _log

BD20_POLL_INTERVAL = 3
BD20_HARD_TIMEOUT = 3600
# U6a: preflight backoff — 5s first check interval, doubling, 30s cap.
PREFLIGHT_POLL_START = 5.0
PREFLIGHT_POLL_CAP = 30.0


def _wait_for_endpoint(url: str, budget: float, logs_dir: Path | None) -> None:
    """U6a: block until the model endpoint answers, within ``budget`` seconds.

    Checks immediately, then re-checks with backoff (5 → 10 → 20 → 30s cap).
    Raises TimeoutError when the budget is spent — the caller must NOT spawn
    the worker (spending a worker's context on a dead endpoint is what lost
    three runs in one day).
    """
    deadline = time.monotonic() + budget
    poll = PREFLIGHT_POLL_START
    check = 0
    while True:
        check += 1
        if _net.endpoint_reachable(url):
            if check > 1 and logs_dir:
                _log(logs_dir, f"U6a: endpoint {url} reachable after {check} checks")
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if logs_dir:
            _log(
                logs_dir,
                f"U6a: endpoint {url} unreachable (check {check}) — "
                f"next check in {min(poll, remaining):.0f}s",
            )
        time.sleep(min(poll, remaining))
        poll = min(poll * 2, PREFLIGHT_POLL_CAP)
    raise TimeoutError(
        f"Model endpoint {url} unreachable for {budget:.0f}s — worker not started"
    )


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
    log_holder: dict[str, str] | None = None,
    preflight_timeout: float | None = None,
    no_output_timeout: float | None = None,
    opencode_config: str | Path | None = None,
    log_name: str | None = None,
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
        log_holder: optional dict populated with ``{"log_path": ...}`` —
            the worker log file this run appends to. Lets callers inspect
            the log after the fact (U6b network-failure classification).
        preflight_timeout: U6a — seconds to wait for the model endpoint
            (from ``--model provider/id`` in ``cmd``) before spawning.
            None → :data:`_net.PREFLIGHT_TIMEOUT_DEFAULT` (600). 0 → skip.
        no_output_timeout: U6c — watchdog: kill the worker when its log
            is not updated for this many seconds. None → :data:`_net.
            NO_OUTPUT_TIMEOUT_DEFAULT` (900). 0 → disabled.
        opencode_config: path to opencode.json for preflight provider
            lookup. None → the XDG opencode.json.
        log_name: AUD16-06 — explicit worker log filename (e.g.
            ``awf-{role}-{todo_id}.out``). The caller knows role and todo_id
            exactly, so the name no longer is guessed from argv (which used
            to produce three forms the dashboard glob could not match).
            None → ``worker-output.out``.
    """
    watch_paths = watch_paths or []
    # KAUD-4: handle hard_timeout=None (use default)
    if hard_timeout is None:
        hard_timeout = BD20_HARD_TIMEOUT
    if preflight_timeout is None:
        preflight_timeout = _net.PREFLIGHT_TIMEOUT_DEFAULT
    if no_output_timeout is None:
        no_output_timeout = _net.NO_OUTPUT_TIMEOUT_DEFAULT

    pre_existing, snapshot = _build_pre_snapshot(watch_paths, watch_new_glob, logs_dir)

    # U6a: preflight BEFORE any spawn — a dead model endpoint must not burn
    # a worker's context. Skipped when the cmd has no --model, the provider
    # is unknown, or the provider has no baseURL (cloud).
    if preflight_timeout > 0:
        model_spec = _net.parse_model_from_cmd(cmd)
        if model_spec:
            if opencode_config:
                oc_path = Path(opencode_config)
            else:
                from . import xdg

                oc_path = xdg.opencode_config_file()
            endpoint_url = _net.model_endpoint_url(model_spec, oc_path)
            if endpoint_url:
                _wait_for_endpoint(endpoint_url, preflight_timeout, logs_dir)

    from ._env import _pdeathsig_preexec
    # KAUD-9: redirect worker stdout/stderr to log file instead of inheriting.
    # Prevents mixing worker output with awf-start.out.
    worker_log = None
    log_path: Path | None = None
    if logs_dir and logs_dir.is_dir():
        # AUD16-06: explicit log name from the caller — run_agent_stage knows
        # role and todo_id exactly. No more guessing from argv (which used to
        # produce three naming forms the dashboard glob could not match).
        # AUD14-04: log_name is public input — keep only the last path
        # component and replace anything outside a safe charset, so
        # "../agent-pwn" cannot escape logs_dir.
        if log_name:
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(log_name).name)
            if not safe_name.endswith(".out"):
                safe_name += ".out"
            log_path = (logs_dir / safe_name).resolve()
        else:
            log_path = (logs_dir / "worker-output.out").resolve()
        if not log_path.is_relative_to(logs_dir.resolve()):
            log_path = (logs_dir / "worker-output.out").resolve()
        # dogfood-11: APPEND instead of overwrite — a retried stage used to
        # erase the previous run's log, so post-mortems lost what the worker
        # actually wrote. Marker line delimits runs for forensics.
        import os as _os
        worker_log = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
        worker_log.write(
            f"\n===== awf run {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"(pid {_os.getpid()}) =====\n"
        )
        worker_log.flush()
        # P2: restrict worker log permissions (may contain sensitive output)
        _os.chmod(log_path, 0o600)
        if log_holder is not None:
            log_holder["log_path"] = str(log_path)
            # U6b: byte offset of this run's first byte. The log is
            # append-mode, so classification of THIS run's death must start
            # here — an earlier run's network marker is not this run's failure.
            log_holder["log_start_offset"] = _os.fstat(worker_log.fileno()).st_size
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env or awf_subprocess_env(),
            stdout=worker_log if worker_log else None,
            stderr=subprocess.STDOUT if worker_log else None,
            preexec_fn=_pdeathsig_preexec,
            # AUD04-06: own process group, so the hard timeout can kill the
            # whole tree (MCP servers, node children), not just opencode.
            **_proc.start_new_session_kwargs(),
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

            # U6c: watchdog — a worker that stops producing output is hung.
            # Kill the tree well before the hard timeout, but never after a
            # signal has been seen (the worker is logically done and may
            # still be flushing buffers — the hard timeout covers that case).
            if (
                signal_seen_at is None
                and worker_log is not None
                and no_output_timeout > 0
            ):
                try:
                    mtime = log_path.stat().st_mtime
                except OSError:
                    mtime = None
                if mtime is not None:
                    silent_for = time.time() - mtime
                    if silent_for > no_output_timeout:
                        if logs_dir:
                            _log(
                                logs_dir,
                                f"U6c: worker silent for {silent_for / 60:.0f} min — "
                                f"killing process tree (pid={proc.pid})",
                            )
                        _proc.kill_process_tree(proc)
                        raise TimeoutError(
                            f"Worker produced no output for "
                            f"{silent_for / 60:.0f} min (watchdog) — "
                            "process tree killed"
                        )

            if now >= deadline:
                if logs_dir:
                    _log(
                        logs_dir,
                        f"BD-20: hard timeout reached, killing process group (pid={proc.pid})",
                    )
                _proc.kill_process_tree(proc)
                if signal_seen_at is not None:
                    # AUD04-06: the signal file is already on disk — the work
                    # is logically done. Consume it instead of raising: the
                    # caller picks the signal up and moves on (the hung tree
                    # is already dead).
                    if logs_dir:
                        _log(
                            logs_dir,
                            "BD-20: signal already detected — treating stage as done",
                        )
                    return subprocess.CompletedProcess(cmd, 0)
                raise TimeoutError(
                    f"Subprocess did not produce signal within {hard_timeout}s"
                )

            time.sleep(BD20_POLL_INTERVAL)
    finally:
        if worker_log:
            worker_log.close()
