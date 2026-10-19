"""Shared stage-log diagnostics for a worker that ended without a signal.

M2 (BACKLOG, 20-21): three architect sessions in 0224 ended without a
signal and the stage log held no trace of why. The normal exit path
(``agent_stage``) and the engine's kill paths (``signal_watch``) must
write the SAME line — hence one builder here, not a copy next to each
caller.

The line is observational only: it never changes retry policy.
"""
from __future__ import annotations

import re
import signal as _signals
from pathlib import Path

from . import _net


def exit_signal_name(rc: int) -> str:
    """Signal that ended the worker, as a name — "none" for a normal exit.

    Negative rc is the raw signal (Popen convention); 128+N is the shell
    convention (137 = SIGKILL/RSS kill). The name keeps the diagnostic
    readable without a lookup table on the reader's side.
    """
    num = -rc if rc < 0 else (rc - 128 if rc > 128 else None)
    if num is None or not 0 < num < _signals.NSIG:
        return "none"
    try:
        return _signals.Signals(num).name
    except ValueError:
        # TODO-0226 (F1): rc>192 yields num>NSIG-1 (255 -> 127, 200 -> 72).
        # Those are exit codes, not signals — a fabricated "SIG127" sends
        # the reader after a signal that never existed.
        return "none"


def classify_worker_exit(rc: int, tail: str) -> str:
    """TODO-0226: why the worker ended silently — one of four classes.

    ``network`` keeps the EXISTING classification (``_net`` markers) first,
    so a network death still drives its own backoff retry. ``oom-like`` is
    a memory-killer sign: rc 137 (128+SIGKILL), a raw SIGKILL, or the
    shell's ``Killed`` line. Everything else is a plain silent exit
    (model stopped answering / output limit / forgot the signal).
    """
    if _net.is_network_failure(tail or ""):
        return "network"
    if rc in (137, -9) or re.search(r"(?m)^Killed\b", tail or ""):
        return "oom-like"
    return "silent"


def worker_log_tail(log_holder: dict[str, str]) -> str:
    """This run's worker-log tail (append-mode log: start offset matters).

    Shared by the death classifier and the continue-push progress block:
    without the offset an earlier run's marker would leak into this run.
    """
    log_path_str = log_holder.get("log_path", "")
    if not log_path_str:
        return ""
    start_offset = int(log_holder.get("log_start_offset") or 0)
    return _net.read_log_tail(Path(log_path_str), start_offset=start_offset)


def worker_exit_line(
    rc: int,
    duration_sec: float,
    tail: str = "",
    *,
    signal_present: bool = False,
    cls: str | None = None,
) -> str:
    """TODO-0226: the stage-log diagnostic for a worker's silent death.

    "" when the worker produced a signal — that is a normal finish, not a
    silent death (U6b). ``cls`` lets an engine kill path label its own
    cause (``timeout``) instead of guessing from an rc it synthesized.
    """
    if signal_present:
        return ""
    return (
        f"worker-exit: rc={rc} signal={exit_signal_name(rc)} "
        f"duration={duration_sec:.0f}s class={cls or classify_worker_exit(rc, tail)}"
    )
