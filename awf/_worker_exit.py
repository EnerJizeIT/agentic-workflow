"""Shared stage-log diagnostics for a worker that ended without a signal.

M2 (BACKLOG, 20-21): three architect sessions in 0224 ended without a
signal and the stage log held no trace of why. The normal exit path
(``agent_stage``) and the engine's kill paths (``signal_watch``) must
write the SAME line — hence one builder here, not a copy next to each
caller.

The line is observational only: it never changes retry policy.
"""
from __future__ import annotations

import signal as _signals
from collections.abc import Callable
from pathlib import Path

from . import _net


def normalize_signal_name(name: str | None) -> str | None:
    """PA2: the ONE place that turns a signal FILE name into a signal name.

    ``_detect_new_signal`` returns the file name (``DONE-TODO-0001.ready``),
    while ``read_signal_for_todo`` already strips the suffix. Both flow into
    logs and the supervisor, so a single normalizer keeps one spelling:
    ``DONE-TODO-0001``. ``.ready`` goes first, then the BD-21 ``.md`` typo
    form (``DONE-TODO-0001.md.ready``).
    """
    if not name:
        return name
    base = name[: -len(".ready")] if name.endswith(".ready") else name
    if base.endswith(".md"):
        base = base[: -len(".md")]
    return base


def signal_present_line(name: str | None) -> str:
    """CARRY IMP1 (R1): the ONE text for "a signal is already on disk".

    Both signal_watch places (late signal after a kill, hard timeout with a
    seen signal) and the normal exit path use it — a second wording without
    the signal name was the defect (AC1/AC3). PA2: the name is normalized
    here, so a watcher file name and the outbox reader print the same text.
    """
    return f"U6b: signal present ({normalize_signal_name(name) or 'unknown'})"


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
    """TODO-0226 / CARRY IMP1: why the worker ended silently.

    Delegates the provider-failure classes to ``_net.classify_failure`` so
    the ``worker-exit:`` line and the engine's retry decision can never
    disagree (R12). ``transient`` (5xx/429/overloaded) keeps the retry
    budget of ``network`` — the diagnostic says ``network`` too, matching
    the requirement's wording ("перегрузка — сеть"). ``auth`` is its own
    class: a dead credential stops the stage with a BLOCKED note.
    """
    failure = _net.classify_failure(tail or "", rc)
    if failure == "auth":
        return "auth"
    if failure in _net.RETRY_FAILURE_CLASSES:
        return "network"
    if _net.is_oom_exit(rc, tail or ""):
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
    tail_fn: Callable[[], str] | None = None,
    signal_present: bool = False,
    cls: str | None = None,
) -> str:
    """TODO-0226: the stage-log diagnostic for a worker's silent death.

    "" when the worker produced a signal — that is a normal finish, not a
    silent death (U6b). CARRY IMP1 (R1): the tail is LAZY. ``tail_fn`` is
    not called when ``signal_present`` is true — reading a 64 KiB log to
    throw the result away was waste on every normal finish. ``cls`` lets an
    engine kill path label its own cause (``timeout``) instead of guessing
    from an rc it synthesized.
    """
    if signal_present:
        return ""
    if tail_fn is not None:
        tail = tail_fn() or ""
    return (
        f"worker-exit: rc={rc} signal={exit_signal_name(rc)} "
        f"duration={duration_sec:.0f}s class={cls or classify_worker_exit(rc, tail)}"
    )
