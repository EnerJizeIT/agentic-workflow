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

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import _net, _proc, paths
from ._env import awf_subprocess_env
from ._log import log as _log
from ._worker_exit import signal_present_line, worker_exit_line
from .infra import managed_fs

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


def _log_timeout_exit(
    logs_dir: Path | None, run_started: float, rc: int | None,
) -> None:
    """TODO-0226: stage-log diagnostic for a watchdog/hard-timeout kill.

    One builder with the normal exit path (``_worker_exit``) — the line
    cannot drift. ``rc`` is the reaped worker's real return code: the kill
    is TERM → grace → KILL, so the child may have died from SIGTERM and a
    hardcoded SIGKILL would name a signal that never landed. ``None`` (an
    unkillable tree) falls back to the KILL we sent.
    """
    if logs_dir:
        _log(
            logs_dir,
            worker_exit_line(
                -9 if rc is None else rc,
                time.monotonic() - run_started,
                cls="timeout",
            ),
        )


def _late_signal_done(
    cmd: list[str],
    late: str | None,
    signal_holder: dict[str, str] | None,
    logs_dir: Path | None,
) -> subprocess.CompletedProcess | None:
    """TODO-0226 (F2): a signal found after a kill is a normal finish.

    All three kill paths (both watchdog branches, hard timeout) share this
    decision: the signal landed while the tree was being killed, so the
    stage is done — not a ``class=timeout`` death.
    """
    if not late:
        return None
    if signal_holder is not None:
        signal_holder["signal"] = late
    if logs_dir:
        _log(
            logs_dir,
            signal_present_line(late),
        )
    return subprocess.CompletedProcess(cmd, 0)


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
    new_glob_accept: Callable[[Path, str], bool] | None = None,
) -> str | None:
    """Check if a new signal file appeared since subprocess start.

    Returns the signal filename if detected, None otherwise.
    Checks concrete watch_paths first, then glob pattern.

    TODO-0213 (REVIEW fix): ``new_glob_accept`` (optional) is a
    content filter for the glob — a new file counts as a signal only when
    the filter accepts it (the replan watcher: only a replan_for-marked
    TODO ends the wait; an unmarked mid-run dispatch must not).
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
            for name in sorted(new_files, key=_sort_key_by_numeric_id):
                if new_glob_accept is None or new_glob_accept(watch_dir, name):
                    return name

    return None


def run_subprocess_until_signal(
    cmd: list[str],
    cwd: str | Path,
    watch_paths: list[Path] | None = None,
    watch_new_glob: tuple[Path, str] | None = None,
    new_glob_accept: Callable[[Path, str], bool] | None = None,
    logs_dir: Path | None = None,
    hard_timeout: int = BD20_HARD_TIMEOUT,
    env: dict[str, str] | None = None,
    signal_holder: dict[str, str] | None = None,
    log_holder: dict[str, str] | None = None,
    preflight_timeout: float | None = None,
    no_output_timeout: float | None = None,
    opencode_config: str | Path | None = None,
    log_name: str | None = None,
    on_spawn: Callable[[int], None] | None = None,
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
        new_glob_accept: TODO-0213 (REVIEW fix) — optional content filter
            ``(watch_dir, name) -> bool`` for the glob: a new file counts
            as a signal only when accepted (the replan watcher accepts
            only replan_for-marked TODOs; None — every new file fires).
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
        on_spawn: RUN8 #2 — optional callback invoked with the worker pid
            right after Popen (BEFORE the wait loop). Callers use it to
            record the pid in state: the worker lives in its own process
            group, so a pipeline-only kill leaves it running (the
            2026-09-23 orphan incident). Best-effort: a raising callback
            is swallowed — recording must never break the run.
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
            target = logs_dir / safe_name
        else:
            target = logs_dir / paths.WORKER_FALLBACK_LOG
        # R1.1/S9-03: open through the managed no-follow helper. A symlinked
        # logs dir, a symlinked leaf (the audited fallback bypass — the
        # final resolve() ran unchecked) AND a symlinked INTERMEDIATE
        # (``.agentic``) are refused BEFORE any write; the file is born
        # 0600; a hardlinked legacy log is never appended to or chmod-ed (a
        # fresh safe sibling is used instead). Resolve-then-open is not
        # enough — the type is re-checked on the opened fd (fstat).
        # REVIEW fix (F1): pass the project root always. The old code
        # nulled the anchor when ``logs_dir`` escaped the (resolved) cwd —
        # but an escape IS the S9-03 signal, so nulling it disabled the
        # intermediate-component check exactly when ``.agentic`` was a
        # symlink out of the project. ``open_append_log`` refuses the
        # escape instead of letting it through.
        anchor = Path(cwd).resolve()
        log_fd, log_path = managed_fs.open_append_log(target, root=anchor)
        worker_log = os.fdopen(log_fd, "a", encoding="utf-8")
        # dogfood-11: APPEND instead of overwrite — a retried stage used to
        # erase the previous run's log, so post-mortems lost what the worker
        # actually wrote. Marker line delimits runs for forensics.
        worker_log.write(
            f"\n===== awf run {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} "
            f"(pid {os.getpid()}) =====\n"
        )
        worker_log.flush()
        if log_holder is not None:
            log_holder["log_path"] = str(log_path)
            # U6b: byte offset of this run's first byte. The log is
            # append-mode, so classification of THIS run's death must start
            # here — an earlier run's network marker is not this run's failure.
            log_holder["log_start_offset"] = os.fstat(worker_log.fileno()).st_size
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
        if on_spawn is not None:
            # RUN8 #2: record the worker pid while it is provably OUR child
            # (the caller writes it to state for kill_pipeline).
            try:
                on_spawn(proc.pid)
            except Exception:
                pass  # best-effort — the run goes on without the record
        deadline = time.monotonic() + hard_timeout
        run_started = time.monotonic()
        signal_seen_at: float | None = None

        def _late_signal() -> str | None:
            """TODO-0226 (F2): re-read the outbox after a kill.

            The kill paths run AFTER this iteration's
            ``_detect_new_signal`` (and the TERM → grace → KILL window is
            seconds long); a DONE written in that window is on disk but
            unseen. Reporting it as ``class=timeout`` would call a finished
            stage a silent death and stop the pipeline with the work
            already done — the same false diagnosis M2 exists to end.
            """
            return _detect_new_signal(
                watch_paths, pre_existing, watch_new_glob, snapshot,
                new_glob_accept,
            )
        # U6c/TODO-0180: CPU-aware watchdog state. ``last_cpu`` — the
        # worker tree's total CPU (utime+stime of the whole tree) at the
        # previous reading, taken only while the log is past the silence
        # threshold; ``last_cpu_busy_at`` — the wall moment CPU growth was
        # last observed. It refreshes the effective silence so a
        # busy-but-quiet tree (a silent full test run) is not killed as
        # hung.
        last_cpu: float | None = None
        last_cpu_busy_at: float | None = None

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
                    new_glob_accept,
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
            # CPU-aware (TODO-0180): silence alone is not a hang — a quiet
            # long command (a full test run) can burn CPU for tens of
            # minutes without a log line. The kill requires the log silence
            # AND a frozen tree CPU: any growth between two readings
            # refreshes the effective silence and the watchdog keeps
            # watching (the stage hard timeout stays the last resort).
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
                    # Effective silence: the log's quiet time, restarted by
                    # the last observed CPU growth (a busy tree is not
                    # silent, whatever its log says).
                    last_activity = mtime
                    if last_cpu_busy_at is not None:
                        last_activity = max(last_activity, last_cpu_busy_at)
                    silent_for = time.time() - last_activity
                    if silent_for > no_output_timeout:
                        cpu_now = _proc.cpu_time_of_tree(proc.pid)
                        if cpu_now is None:
                            # /proc unreadable — the tree cannot be proven
                            # busy; the legacy log-only decision stands.
                            if logs_dir:
                                _log(
                                    logs_dir,
                                    f"U6c: worker silent for {silent_for / 60:.0f} min — "
                                    f"killing process tree (pid={proc.pid})",
                                )
                            _proc.kill_process_tree(proc)
                            # TODO-0226: the watchdog death is a silent one —
                            # leave the same diagnostic the normal exit path
                            # writes (shared builder in _worker_exit). A
                            # signal that landed in the last window is a
                            # normal finish, not a death (F2).
                            done = _late_signal_done(
                                cmd, _late_signal(), signal_holder, logs_dir,
                            )
                            if done is not None:
                                return done
                            _log_timeout_exit(logs_dir, run_started, proc.returncode)
                            raise TimeoutError(
                                f"Worker produced no output for "
                                f"{silent_for / 60:.0f} min (watchdog) — "
                                "process tree killed"
                            )
                        if last_cpu is None:
                            # First reading in the silent zone: store it and
                            # give the tree one more poll to prove itself.
                            last_cpu = cpu_now
                        elif cpu_now > last_cpu:
                            # The tree burned CPU since the last reading —
                            # a silent long command, not a hang. Refresh the
                            # effective silence; keep watching.
                            last_cpu = cpu_now
                            last_cpu_busy_at = time.time()
                        else:
                            if logs_dir:
                                _log(
                                    logs_dir,
                                    f"U6c: worker silent for {silent_for / 60:.0f} min "
                                    f"and tree CPU shows no progress — "
                                    f"killing process tree (pid={proc.pid})",
                                )
                            _proc.kill_process_tree(proc)
                            done = _late_signal_done(
                                cmd, _late_signal(), signal_holder, logs_dir,
                            )
                            if done is not None:
                                return done
                            _log_timeout_exit(logs_dir, run_started, proc.returncode)
                            raise TimeoutError(
                                f"Worker produced no output for "
                                f"{silent_for / 60:.0f} min and tree CPU "
                                f"shows no progress (watchdog) — "
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
                    # CARRY IMP1 (R1): name the signal — the holder has it;
                    # without a holder re-read the outbox, else "unknown".
                    # The send-off is the shared ``_late_signal_done`` — the
                    # same decision the other two kill paths take.
                    late_name = (
                        signal_holder.get("signal") if signal_holder else None
                    ) or _late_signal() or "unknown"
                    done = _late_signal_done(
                        cmd, late_name, signal_holder, logs_dir,
                    )
                    if done is not None:
                        return done
                # TODO-0226 (F2): the TERM → grace → KILL kill is another
                # window a signal can land in — re-read before calling the
                # stage a timeout death.
                done = _late_signal_done(
                    cmd, _late_signal(), signal_holder, logs_dir,
                )
                if done is not None:
                    return done
                _log_timeout_exit(logs_dir, run_started, proc.returncode)
                raise TimeoutError(
                    f"Subprocess did not produce signal within {hard_timeout}s"
                )

            time.sleep(BD20_POLL_INTERVAL)
    finally:
        if worker_log:
            worker_log.close()
