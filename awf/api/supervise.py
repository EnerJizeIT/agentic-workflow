"""TODO-0242 (P0-2): ``awf supervise`` — the watchdog for a dead engine.

The incident of 2026-10-23: the engine died six times in one run and nobody
restarted it; the run sat "active" until a human noticed. This module is the
mechanical restart: while the run has an active unit, a dead engine (the one
fact :func:`awf._engine_liveness.status_engine_dead` owns) triggers
``awf_continue(todo_id=...)`` with a BOUNDED budget per run.

The budget is the safety property: a crash-loop must not burn the machine
forever. After ``max_restarts`` the watchdog stops with a report and leaves
the run exactly as it found it — the human decides.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from .. import paths, run_state
from .._log import log as _log
from ..infra import managed_fs
from ..pipeline_state import read_state


def supervise_run(
    project_dir: Path,
    *,
    max_restarts: int = 5,
    pause_seconds: float = 30.0,
    iterations: int | None = None,
    sleep=time.sleep,
) -> dict:
    """Watch an active run; restart a dead engine up to ``max_restarts``.

    Args:
        project_dir: awf project root.
        max_restarts: restarts allowed for THIS run (default 5).
        pause_seconds: wait between checks (default 30).
        iterations: optional loop bound for tests; None = until the run ends
            or the budget is exhausted.
        sleep: injected sleep (tests pass a no-op).

    Returns:
        Dict with: action (stopped/exhausted/no_run/error), restarts,
        stop_reason, last_todo, message, log_file.
    """
    from ._liveness import engine_dead_now
    from .pipeline import continue_pipeline

    resolved = Path(project_dir).resolve()
    logs = paths.logs_dir(resolved)
    log_file = logs / "awf-supervise.log"
    restarts = 0
    cycles = 0
    last_todo = ""

    while True:
        if iterations is not None and cycles >= iterations:
            return _answer(
                "stopped", restarts, last_todo,
                "iteration budget exhausted (test bound)", log_file,
            )
        state = run_state.read_run(resolved)
        if not state or not state.get("active"):
            return _answer(
                "no_run", restarts, last_todo,
                "no active run — nothing to supervise", log_file,
            )
        current = str(state.get("current") or "")
        dead = engine_dead_now(resolved, read_state(resolved))
        if dead is not None:
            if restarts >= max_restarts:
                return _answer(
                    "exhausted", restarts, last_todo or dead.todo_id,
                    f"restart budget exhausted ({max_restarts}) — the engine "
                    "keeps dying; inspect the logs and the run yourself",
                    log_file,
                )
            last_todo = dead.todo_id or current
            try:
                launch = continue_pipeline(
                    resolved, todo_id=last_todo, background=True
                )
            except Exception as exc:  # noqa: BLE001 — report, do not crash
                restarts += 1
                _append_log(log_file, f"supervise: restart #{restarts} failed: {exc}")
                return _answer(
                    "error", restarts, last_todo,
                    f"restart failed: {exc}", log_file,
                )
            # TODO-0242 REVIEW (F7): only a real launch is a restart. A
            # no-op (another launch already in progress / nothing to
            # resume) did not touch the engine — counting it would burn
            # the budget and report a false crash-loop.
            mode = ""
            if isinstance(launch, dict):
                mode = str(launch.get("run_mode") or "")
            else:
                mode = str(getattr(launch, "run_mode", "") or "")
            if mode == "error":
                # TODO-0242 REVIEW (G2): a launch that failed to start the
                # engine IS a restart attempt (the incident's shape). The
                # exception path above already spends budget; the returned
                # ``error`` mode must not loop forever.
                restarts += 1
                _append_log(
                    log_file, f"supervise: restart #{restarts} (error)"
                )
                if restarts >= max_restarts:
                    return _answer(
                        "error", restarts, last_todo,
                        f"restart budget exhausted ({max_restarts}) — every "
                        "launch failed to start the engine; inspect the logs "
                        "and the config yourself",
                        log_file,
                    )
            elif mode not in {"background", "foreground"}:
                _append_log(
                    log_file,
                    f"supervise: nothing to restart (run_mode={mode or '?'})",
                )
            else:
                restarts += 1
                _append_log(log_file, f"supervise: restart #{restarts} "
                                      f"({dead.line})")
        cycles += 1
        sleep(pause_seconds)


def _answer(action: str, restarts: int, todo_id: str, reason: str, log_file: Path) -> dict:
    return {
        "action": action,
        "restarts": restarts,
        "last_todo": todo_id,
        "stop_reason": reason,
        "log_file": str(log_file),
        "message": f"supervise: {reason} (restarts: {restarts})",
    }


def _append_log(path: Path, line: str) -> None:
    """Append one line through the managed log opener (no-follow, 0600).

    A bare ``Path.open("a")`` followed a symlinked ``.agentic``/``logs``
    outside the project — the F-B/F2 class every other log writer already
    delegates to ``managed_fs``. The opener also creates the ``logs``
    directory inside its no-follow walk, so no separate ``mkdir`` is owed.
    A refused surface degrades to ``orchestrator.log`` with the same line:
    the watchdog never breaks a run because a log path is unsafe.
    """
    try:
        fd, _actual = managed_fs.open_append_log(path, create=True)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except (OSError, managed_fs.ManagedPathError):
        _log(path.parent, f"supervise: cannot append to {path}: {line}")


__all__ = ["supervise_run"]
