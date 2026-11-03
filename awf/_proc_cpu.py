"""Read-only CPU probe for a process tree (split from :mod:`awf._proc`).

The U6c watchdog asks one question — is the worker's tree burning CPU while
its log is silent — and it never signals anything. It lives apart from the
signal helpers so :mod:`awf._proc` keeps one concern (the safety-guarded
kill paths) and stays inside the file budget.
"""
from __future__ import annotations

import os

from ._proc import snapshot_rows


def cpu_time_of_tree(pid: int) -> float | None:
    """Total CPU time (utime+stime, seconds) of ``pid`` and all of its
    descendants, read from /proc (Linux); None when /proc is unavailable
    or ``pid <= 1``.

    U6c watchdog probe (TODO-0180): a worker whose log goes silent may
    still be working — a quiet long command (a full test run) burns CPU
    for tens of minutes without a log line. A busy tree accumulates CPU
    between two readings; a stalled one does not.

    Read-only: no signal is ever sent, and an entry that vanishes
    mid-walk is simply skipped. ``pid <= 1`` refuses like
    :func:`awf._proc.child_pids` — init's tree is never measured (the
    2026-09-20 session-kill rule: nothing may ever target pid 1).
    """
    if pid is None or pid <= 1:
        return None
    stats = snapshot_rows()
    if not stats:
        return None
    children: dict[int, list[int]] = {}
    for row in stats.values():
        if row.ppid is not None:
            children.setdefault(row.ppid, []).append(row.pid)
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        hz = 100  # historical Unix clock rate — last-resort fallback
    total = 0.0
    seen: set[int] = set()
    queue: list[int] = [pid]
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        row = stats.get(current)
        if row is not None and len(row.fields) >= 13:
            try:
                # /proc stat fields after comm: utime is the 14th overall
                # (index 11 here), stime the 15th (index 12).
                total += (int(row.fields[11]) + int(row.fields[12])) / hz
            except ValueError:
                pass
        queue.extend(children.get(current, ()))
    return total


__all__ = ["cpu_time_of_tree"]
