"""Process-group aware subprocess helpers (AUD04-06, AUD14-03).

``subprocess.run(timeout=...)`` and ``Popen.kill()`` only reach the direct
child. Its descendants (MCP servers, xdist workers, node children) keep
running as orphans and can hold ports, locks, and log files.

Rule: every awf subprocess with a timeout or kill path is started in its
own session (``start_new_session=True``) and killed through
:func:`kill_process_tree` so the whole group dies together.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

KILL_GRACE_SECONDS = 3.0


def _own_session() -> bool:
    """POSIX-only: on Windows Popen does not support session/process groups."""
    return sys.platform != "win32"


def start_new_session_kwargs() -> dict:
    """Popen kwargs that give the child its own process group (POSIX only)."""
    if _own_session():
        return {"start_new_session": True}
    return {}


def kill_process_tree(proc: subprocess.Popen, grace: float = KILL_GRACE_SECONDS) -> None:
    """Kill the whole process group of ``proc`` and reap it.

    SIGTERM the group → wait ``grace`` seconds → SIGKILL the remainder →
    wait. Covers: child already gone, group already gone, child survives
    SIGTERM (hang), and child exited while grandchildren still run in its
    group (the orphan case from AUD04-06/AUD14-03).
    """
    pgid = None
    if _own_session():
        try:
            candidate = os.getpgid(proc.pid)
        except (OSError, ProcessLookupError):
            candidate = None
        # Only kill a group we started ourselves: with start_new_session the
        # child is its own group leader (pgid == pid). An inherited group
        # would be the PARENT's — killing it is catastrophic. The equality
        # check also defuses fake Popen pids in unit tests — except pid 1:
        # init IS its own group leader (getpgid(1) == 1), so killpg(1, …)
        # becomes kill(-1, …) — SIGTERM/SIGKILL to every process the caller
        # may signal (the whole uid session). Hence the pid > 1 guard
        # (2026-09-20 session-kill incident, full pytest run 3/3).
        if candidate is not None and candidate == proc.pid and proc.pid > 1:
            pgid = candidate

    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    else:
        proc.kill()

    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass

    if pgid is not None:
        # Group probe: signal 0 fails only when the group has no members
        # left. The direct child may be dead while descendants survive.
        try:
            os.killpg(pgid, 0)
            os.killpg(pgid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass


def _stat_state_and_fields(pid: int) -> tuple[str | None, list[str]]:
    """(state, post-comm fields) from ``/proc/<pid>/stat``; (None, []) if
    unreadable. comm may contain spaces/parens — split after the last ')'.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("ascii", "replace")
    except OSError:
        return None, []
    rpar = raw.rfind(")")
    if rpar == -1:
        return None, []
    fields = raw[rpar + 2 :].split()
    return (fields[0] if fields else None), fields


def _pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` probe + the ZOMBIE check: a defunct process still
    answers signal 0 but does no work — it counts as dead. Without /proc
    the signal probe stands (zombies are reaped fast by a living parent's
    reaper in practice; documented degradation)."""
    try:
        os.kill(pid, 0)
    except (OverflowError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    state, fields = _stat_state_and_fields(pid)
    if state is None:
        return True  # no /proc — trust the signal probe
    return state not in ("Z", "X")


def ppid_of(pid: int) -> int | None:
    """Parent pid from ``/proc/<pid>/stat`` (Linux); None elsewhere.

    Best-effort: an unreadable stat (process gone, non-Linux, permission)
    is None — callers treat it as "parentage cannot be proven", never as
    a signal to kill.
    """
    if pid is None or pid <= 1:
        return None
    _state, fields = _stat_state_and_fields(pid)
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def child_pids(pid: int) -> list[int]:
    """Direct children of ``pid`` from /proc (Linux); [] elsewhere or on
    any error.

    Best-effort topology probe for kill time: a dead pid or a failed read
    yields an empty list, never an exception — the caller treats the
    result as "no children found".
    """
    if pid is None or pid <= 1:
        return []
    try:
        names = os.listdir("/proc")
    except OSError:
        return []
    kids: list[int] = []
    for name in names:
        if name.isdigit() and ppid_of(int(name)) == pid:
            kids.append(int(name))
    return sorted(kids)


def _live_members_of_group(pgid: int) -> bool | None:
    """Does the process group still have a LIVE (non-zombie) member?

    True: at least one live member. False: the group is empty or holds
    only zombies (defunct members do no work and get reaped by the new
    parent — init after a reparent). None: /proc unavailable — the
    caller falls back to the signal-0 probe.
    """
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    live = False
    for name in names:
        if not name.isdigit():
            continue
        state, fields = _stat_state_and_fields(int(name))
        if state is None or len(fields) < 3:
            continue
        try:
            pgrp = int(fields[2])
        except ValueError:
            continue
        if pgrp == pgid and state not in ("Z", "X"):
            live = True
            break
    return live


def kill_pid_tree(pid: int, grace: float = KILL_GRACE_SECONDS, poll: float = 0.1) -> str:
    """Kill the process tree of ``pid`` — no Popen handle required.

    The pid-based twin of :func:`kill_process_tree` for a process this
    process does not own (we cannot ``wait`` on it — liveness is polled
    with ``kill(pid, 0)`` instead):

    - ``pid <= 1`` → ``"skipped"`` — nothing is touched. The 2026-09-20
      incident guard: signaling process group 1 reaches every process of
      the caller's session — a session-wide kill by another name.
    - a process GROUP is signaled only when ``pid`` is its own group
      leader (``getpgid(pid) == pid`` — our workers, started with
      ``start_new_session``). An inherited group belongs to someone else
      — only the bare pid is signaled, never the group (the same rule as
      :func:`kill_process_tree`).
    - SIGTERM the tree → poll up to ``grace`` seconds → SIGKILL what
      remains → poll again.

    Returns ``"dead"`` (already gone), ``"killed"`` (gone after our
    signals), ``"survived"`` (alive after TERM + KILL — the caller must
    warn the owner with the pid), or ``"skipped"``.
    """
    import time as _time

    if not isinstance(pid, int) or pid <= 1:
        return "skipped"
    if not _pid_alive(pid):
        return "dead"

    pgid: int | None = None
    if _own_session():
        try:
            candidate = os.getpgid(pid)
        except (OverflowError, OSError, ProcessLookupError):
            candidate = None
        if candidate is not None and candidate == pid:
            pgid = candidate

    def _signal_tree(sig: int) -> None:
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except (OverflowError, OSError, ProcessLookupError):
            pass

    def _tree_alive() -> bool:
        try:
            if pgid is not None:
                os.killpg(pgid, 0)
            else:
                os.kill(pid, 0)
        except (OverflowError, ProcessLookupError):
            return False
        except PermissionError:
            # Exists but not signalable — we can neither confirm death
            # nor kill it; treat as alive.
            return True
        # signal 0 also succeeds for ZOMBIES — a defunct member does no
        # work. With /proc readable, require a LIVE (non-zombie) member /
        # pid instead.
        if pgid is not None:
            live = _live_members_of_group(pgid)
            return True if live is None else live
        return _pid_alive(pid)

    _signal_tree(signal.SIGTERM)
    deadline = _time.monotonic() + grace
    while _time.monotonic() < deadline:
        if not _tree_alive():
            return "killed"
        _time.sleep(poll)

    _signal_tree(signal.SIGKILL)
    deadline = _time.monotonic() + grace
    while _time.monotonic() < deadline:
        if not _tree_alive():
            return "killed"
        _time.sleep(poll)
    return "survived"


def run_tree(
    cmd,
    *,
    timeout=None,
    capture_output=False,
    text=False,
    cwd=None,
    **popen_kwargs,
) -> subprocess.CompletedProcess:
    """Like ``subprocess.run``, but a timeout kills the whole process group.

    The child starts in its own session. On ``TimeoutExpired`` the group is
    killed (SIGTERM → grace → SIGKILL) and the exception re-raised — the
    caller contract is identical to ``subprocess.run``.
    """
    popen_kwargs = {**start_new_session_kwargs(), **popen_kwargs}
    pipes: dict = {}
    if capture_output:
        pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    proc = subprocess.Popen(cmd, text=text, cwd=cwd, **pipes, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
