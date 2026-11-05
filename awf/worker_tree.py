"""A44 (TODO-0359): reap a stage worker's tree — grandchildren included.

The engine spawns ``opencode run`` with ``start_new_session`` (own process
group). When the worker exits naturally the engine never signals that
group: ``opencode serve --stdio`` and the MCP servers it started
(``agent_workflow_ui``, ``mcp-atlassian``) keep running as orphans and pile
up across stages.

Rule: a process belongs to the worker tree when it carries the stage's
``AWF_STAGE_ANCHOR`` env marker, is a member of the worker's OWN process
group (``pgrp == pgid`` recorded at spawn), or descends from the worker by
``ppid``. The marker is what reaches ``setsid`` descendants: ``opencode
serve --stdio`` and the MCP servers each live in their own session, so
after the worker exits neither group nor parentage points at them.

pid 1 and the caller's own group / ancestry are never signaled — the
2026-09-20 session-kill incident rule.

This module owns the stage marker and everything keyed on it: which
processes belong to the tree, what a leftover is, the stage-start check,
and the one place the marker is dropped from a deliberately detached
child. The kill rounds live in :mod:`awf.worker_reap`. The shared process
facts (the ``/proc`` snapshot and stat row, the caller's ancestry chain,
the group safety rule, the kill grace) come from :mod:`awf._proc` — one
reader per fact, so a hardening of the signal guards never has to be
applied twice. Only the report-only reads (a member's command line and
its stage marker) are local: they feed the log line, never a signal
decision.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from ._log import log as _log
from ._proc import (
    caller_chain,
    is_live_state,
    signalable_group,
    snapshot_rows,
    stat_row,
)
from .redact import SECRET_NAME_KEYWORDS

# Stage-exit facade: callers (stage, supervisor, status) import the reap
# names from here; the kill rounds themselves live in ``worker_reap``.
from .worker_reap import (  # noqa: F401 — re-exported on purpose
    _MAX_LOGGED,
    ReapReport,
    reap_stage_tree,
    reap_worker_tree,
)

_MAX_CMD_LEN = 120
# A44: the env marker the worker's whole tree inherits (``_env`` writes it).
ANCHOR_ENV = "AWF_STAGE_ANCHOR"

# cmdline may carry credentials (``--token=…``, ``--secret value``, cookies
# passed as args). Both the ``name=value`` and the ``name value`` spelling
# are masked — a log line is shared, the secret is not. The words come from
# ``redact``'s list; the marker stays ``***`` (a cmdline has no room for more).
_REDACT = re.compile(
    r"(?i)\b([\w-]*(?:" + "|".join(SECRET_NAME_KEYWORDS) + r")[\w-]*)"
    r"(=|\s+)(\S+)"
)


@dataclass(frozen=True)
class ProcInfo:
    """One process from ``/proc`` (Linux) or an equivalent probe."""

    pid: int
    ppid: int | None
    pgid: int | None
    state: str
    comm: str
    cmdline: str

    def label(self) -> str:
        return self.cmdline or self.comm or "?"


def _read_cmdline(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    text = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
    text = _REDACT.sub(r"\1\2***", text)
    return text[:_MAX_CMD_LEN]


def _proc_info(pid: int) -> ProcInfo | None:
    """One live-ish process: identity from ``_proc``, command for the report."""
    row = stat_row(pid)
    if row is None:
        return None
    return ProcInfo(
        pid=pid,
        ppid=row.ppid,
        pgid=row.pgid,
        state=row.state,
        comm=row.comm,
        cmdline=_read_cmdline(pid),
    )


def _scan() -> dict[int, ProcInfo] | None:
    """Snapshot of every readable process, or None when /proc is absent."""
    rows = snapshot_rows()
    if rows is None:
        return None
    result: dict[int, ProcInfo] = {}
    for pid, row in rows.items():
        result[pid] = ProcInfo(
            pid=pid,
            ppid=row.ppid,
            pgid=row.pgid,
            state=row.state,
            comm=row.comm,
            cmdline=_read_cmdline(pid),
        )
    return result


def new_anchor_token() -> str:
    """A unique token for one spawn — the marker the worker's tree inherits."""
    return uuid.uuid4().hex


def _anchor_needle(token: str | None) -> bytes | None:
    """The exact ``/proc/<pid>/environ`` entry for ``token``, None without it."""
    if not token:
        return None
    return f"{ANCHOR_ENV}={token}".encode("ascii", "replace") + b"\x00"


def _carries_anchor(pid: int, needle: bytes) -> bool:
    """Does the process's environment carry our stage marker?

    Inherited through ``exec``, so ``setsid`` children keep it after the
    worker is gone. Unreadable environ (another uid, gone process, no
    /proc) is False — "cannot prove" never means "signal it".
    """
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return needle in raw


def _group_guard(pgid: int | None) -> bool:
    """Group guard — the rule is owned by :func:`awf._proc.signalable_group`."""
    return signalable_group(pgid)


def live_members(
    root_pid: int | None = None,
    pgid: int | None = None,
    token: str | None = None,
) -> list[ProcInfo]:
    """Live (non-zombie) processes of the worker tree, empty when unsure.

    A member is: ``pgrp == pgid`` (the group recorded at spawn), a
    descendant of ``root_pid`` by ``ppid``, or a process whose environment
    carries the stage's ``AWF_STAGE_ANCHOR`` token. The token is what keeps
    ``setsid`` descendants (``opencode serve``, MCP servers) in reach after
    the worker exits and they are reparented to init.

    Anchors are optional; with none usable the answer is ``[]`` — "cannot
    prove ownership" is never a reason to signal anything.
    """
    # A usable group that FAILS the guard is the caller's own session (or
    # the entire user session): refuse the whole walk rather than fall back
    # to parentage and pick up the caller's own children.
    group = pgid if _group_guard(pgid) else None
    if group is None and pgid is not None and pgid > 1:
        return []
    if root_pid == os.getpid():
        return []
    root = root_pid if root_pid is not None and root_pid > 1 else None
    needle = _anchor_needle(token)
    if group is None and root is None and needle is None:
        return []
    scan = _scan()
    if scan is None:
        return []

    foreign = set(caller_chain())
    members: dict[int, ProcInfo] = {}
    for pid, info in scan.items():
        if pid in foreign or pid <= 1:
            continue
        if root is not None and info.ppid == root:
            members[pid] = info
            continue
        if group is not None and info.pgid == group:
            members[pid] = info
            continue
        if needle is not None and _carries_anchor(pid, needle):
            members[pid] = info

    # Walk down from the seeded set: a grandchild whose parent left the
    # group is still ours by parentage (while that parent is in the seed).
    changed = True
    while changed:
        changed = False
        for pid, info in scan.items():
            if pid in members or pid in foreign or pid <= 1:
                continue
            if info.ppid in members:
                members[pid] = info
                changed = True

    return [
        info for info in members.values() if is_live_state(info.state)
    ]


def without_anchor(env: dict[str, str]) -> dict[str, str]:
    """Drop the stage marker from a deliberately detached child's env.

    A background engine (``awf start`` launched from a stage worker / MCP
    tool) and a detached suite run are NOT part of the stage that started
    them — they must outlive it. Leaving the marker in their environment
    would make the stage-exit reap kill them (A44 regression).
    """
    env.pop(ANCHOR_ENV, None)
    return env


def state_anchor(state: dict | None) -> tuple[int, int] | None:
    """``(pid, pgid)`` of the worker the state records; None when unusable.

    The one place the state's anchor rule lives: a missing or bogus group
    degrades to the pid (``start_new_session`` makes them equal), and a
    pid at or below 1 means "no worker to look at".
    """
    if not isinstance(state, dict):
        return None
    pid = state.get("worker_pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    recorded = state.get("worker_pgid")
    pgid = recorded if isinstance(recorded, int) and recorded > 1 else pid
    return pid, pgid


def state_token(state: dict | None) -> str | None:
    """The stage anchor token the state records; None when absent."""
    if not isinstance(state, dict):
        return None
    token = state.get("worker_anchor")
    return token if isinstance(token, str) and token else None


def has_anchor(state: dict | None) -> bool:
    """Does the state record anything usable to look at the worker tree?

    The one rule readers ask: pid/group or the marker token. A surface that
    re-derives it from state keys is how the status line and ``get_status``
    drifted apart (CTX2).
    """
    return state_anchor(state) is not None or state_token(state) is not None


def live_members_of_state(state: dict | None) -> list[ProcInfo]:
    """Live members of the worker tree the state records; [] when unsure."""
    anchor = state_anchor(state)
    token = state_token(state)
    if anchor is None and token is None:
        return []
    pid, pgid = anchor if anchor is not None else (None, None)
    return live_members(pid, pgid, token)


def log_leftovers(
    state: dict | None,
    logs_dir: Path | None,
    *,
    label: str = "previous stage",
) -> int:
    """A44: name live processes the state's recorded worker left behind.

    Read-only — the previous stage's own reap owns the kill. Returns the
    number of live members (0 when the state has no usable anchor).
    """
    try:
        members = live_members_of_state(state)
        if not members or logs_dir is None:
            return len(members)
        labels = ", ".join(
            f"pid={info.pid} ({info.label()})" for info in members[:_MAX_LOGGED]
        )
        _log(
            logs_dir,
            f"A44: leftovers from the {label} "
            f"({state.get('worker_role', '?')}/{state.get('worker_todo', '?')}): "
            f"{len(members)} live process(es): {labels}",
        )
        return len(members)
    except Exception:  # noqa: BLE001 — the check must never stop the stage
        return 0


class SpawnAnchor:
    """The pid and process group of one stage spawn — the reap anchor.

    Pass the instance as the runner's ``on_spawn``: it is called with the
    child pid and remembers both numbers, so the ``finally`` reap works
    even when the wait ends with an exception. ``start_new_session`` makes
    the worker its own group leader, so the group normally equals the pid;
    the ``getpgid`` read keeps that a fact, not an assumption.
    """

    def __init__(self, token: str | None = None) -> None:
        self.pid: int | None = None
        self.pgid: int | None = None
        # A44: the env marker this spawn's tree inherits (see ``_env``).
        self.token = token

    def __call__(self, pid: int) -> None:
        self.pid = pid
        try:
            self.pgid = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            self.pgid = pid

    def usable(self) -> bool:
        return isinstance(self.pid, int) and self.pid > 1


def record_worker_state(
    project_dir: Path,
    logs_dir: Path | None,
    anchor: SpawnAnchor,
    *,
    role: str,
    todo_id: str | None,
) -> None:
    """A44: write this spawn's pid, group and token into pipeline state.

    The writer of exactly the keys :func:`state_anchor` and
    :func:`state_token` read — a stage that records its worker any other
    way drifts from the status/reap readers. Best-effort: a failed write
    only degrades ``awf status`` and the kill fallback.
    """
    from .pipeline_state import write_state

    try:
        write_state(
            project_dir,
            logs_dir=logs_dir,
            worker_pid=anchor.pid,
            worker_pgid=anchor.pgid,
            worker_anchor=anchor.token,
            worker_role=role,
            worker_todo=todo_id,
        )
    except Exception:
        pass


def log_stage_leftovers(
    project_dir: Path,
    logs_dir: Path | None,
    *,
    label: str = "previous stage",
) -> int:
    """A44: a stage's start check — name what the previous worker left.

    Reads the state the stage start happens on; read-only by design (the
    previous stage's own reap owns the kill).
    """
    from .pipeline_state import read_state

    return log_leftovers(read_state(project_dir), logs_dir, label=label)


__all__ = [
    "ANCHOR_ENV",
    "ProcInfo",
    "ReapReport",
    "SpawnAnchor",
    "has_anchor",
    "live_members",
    "live_members_of_state",
    "log_leftovers",
    "log_stage_leftovers",
    "new_anchor_token",
    "record_worker_state",
    "reap_stage_tree",
    "reap_worker_tree",
    "state_anchor",
    "state_token",
    "without_anchor",
]
