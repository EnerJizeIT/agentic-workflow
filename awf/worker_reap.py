"""A44 (TODO-0359): the kill rounds of a finished stage's worker tree.

Split from :mod:`awf.worker_tree` (file budget): that module owns *who* is
in the tree (marker, group, parentage) and the stage-start checks; this one
owns *how* the leftovers are stopped — TERM → grace → KILL, repeated on a
fresh snapshot so a member that forks on the way out cannot leak children.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ._log import log as _log
from ._proc import KILL_GRACE_SECONDS, is_live_state, signal_pid

if TYPE_CHECKING:  # pragma: no cover — types only, no runtime import cycle
    from .worker_tree import ProcInfo, SpawnAnchor

_POLL_INTERVAL = 0.1
_MAX_LOGGED = 10
# A breeding tree is reported as survivors, never chased without a bound.
_MAX_ROUNDS = 3


@dataclass
class ReapReport:
    """What a reap pass did: ``killed`` and the ``survivors`` after signals."""

    killed: list[ProcInfo] = field(default_factory=list)
    survivors: list[ProcInfo] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.killed)


def _alive(infos: list[ProcInfo]) -> list[ProcInfo]:
    """The subset still alive right now (zombies count as dead)."""
    from .worker_tree import _proc_info

    live: list[ProcInfo] = []
    for info in infos:
        current = _proc_info(info.pid)
        if current is not None and is_live_state(current.state):
            live.append(current)
    return live


def _signal(infos: list[ProcInfo], sig: int) -> None:
    """Signal each member through the package's guarded single-pid path."""
    for info in infos:
        signal_pid(info.pid, sig)


def _wait_gone(members: list[ProcInfo], grace: float) -> list[ProcInfo]:
    """Poll ``members`` until they are gone or ``grace`` runs out."""
    deadline = time.monotonic() + grace
    remaining = _alive(members)
    while remaining and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        remaining = _alive(members)
    return remaining


def _reap_round(
    root_pid: int | None,
    pgid: int | None,
    grace: float,
    token: str | None = None,
) -> list[ProcInfo]:
    """One TERM → grace → KILL pass over a fresh snapshot of the tree."""
    from .worker_tree import live_members

    members = live_members(root_pid, pgid, token)
    if not members:
        return []
    _signal(members, signal.SIGTERM)
    remaining = _wait_gone(members, grace)
    if remaining:
        _signal(remaining, signal.SIGKILL)
        _wait_gone(remaining, grace)
    return members


def reap_worker_tree(
    root_pid: int | None,
    pgid: int | None = None,
    *,
    token: str | None = None,
    grace: float = KILL_GRACE_SECONDS,
    logs_dir: Path | None = None,
) -> ReapReport:
    """Kill what is left of a finished stage's worker tree.

    SIGTERM the members → wait ``grace`` → SIGKILL the survivors → wait,
    then look again: a member that ignores SIGTERM and forks on the way out
    leaves children the first snapshot never saw (the restart-loop pile-up
    A44 removes). Rounds are bounded — a tree that keeps breeding is logged
    as survivors, not chased forever. ``token`` is the stage env marker:
    without it, ``setsid`` descendants that lost both group and parentage
    are out of reach.

    Best-effort by design: nothing here raises into the stage, and a tree
    that cannot be attributed is left alone (``ReapReport()``).
    """
    report = ReapReport()
    try:
        seen: dict[int, ProcInfo] = {}
        for _ in range(_MAX_ROUNDS):
            members = _reap_round(root_pid, pgid, grace, token)
            if not members:
                break
            for info in members:
                seen.setdefault(info.pid, info)
            if _alive(members):
                # Survivors of a SIGKILL are unkillable (D state): another
                # round only waits again, it never helps.
                break

        report.survivors = _alive(list(seen.values()))
        survivor_pids = {info.pid for info in report.survivors}
        report.killed = [m for m in seen.values() if m.pid not in survivor_pids]
        if logs_dir:
            _log(logs_dir, report_line(report))
            for info in report.survivors[:_MAX_LOGGED]:
                _log(
                    logs_dir,
                    f"A44: worker process survived TERM+KILL: pid={info.pid} "
                    f"({info.label()})",
                )
        return report
    except Exception as e:  # noqa: BLE001 — cleanup must never stop the stage
        if logs_dir:
            _log(logs_dir, f"A44: worker tree reap failed: {type(e).__name__}: {e}")
        return report


def report_line(report: ReapReport) -> str:
    """One log line: how many were reaped and which (pid + command)."""
    if not report.killed and not report.survivors:
        return "A44: worker tree reaped: 0 process(es) left"
    parts = [
        f"pid={info.pid} ({info.label()})" for info in report.killed[:_MAX_LOGGED]
    ]
    more = (
        f" … +{len(report.killed) - _MAX_LOGGED}"
        if len(report.killed) > _MAX_LOGGED
        else ""
    )
    line = f"A44: worker tree reaped: {len(report.killed)} process(es)"
    if parts:
        line += ": " + ", ".join(parts) + more
    if report.survivors:
        line += f"; {len(report.survivors)} survived"
    return line


def reap_stage_tree(
    anchor: SpawnAnchor,
    logs_dir: Path | None,
    *,
    label: str,
) -> ReapReport:
    """A44: the one exit path every stage uses — reap its worker's tree.

    ``label`` is the ``role/todo`` the log line names. A stage that never
    spawned (no anchor) reaps nothing; the anchor rules of
    :func:`reap_worker_tree` decide what is signalable.
    """
    if not anchor.usable():
        return ReapReport()
    pid = anchor.pid
    pgid = anchor.pgid if isinstance(anchor.pgid, int) and anchor.pgid > 1 else pid
    if logs_dir is not None:
        _log(logs_dir, f"A44: reaping worker tree of {label} (pid={pid})")
    return reap_worker_tree(pid, pgid, token=anchor.token, logs_dir=logs_dir)


__all__ = [
    "ReapReport",
    "reap_stage_tree",
    "reap_worker_tree",
    "report_line",
]
