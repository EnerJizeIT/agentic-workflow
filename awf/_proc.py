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
