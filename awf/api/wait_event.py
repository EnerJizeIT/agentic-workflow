"""Supervisor wake-up: block until pipeline event (no more polling loops).

Replaces the ``while True: sleep(30); awf_status()`` pattern with a
single blocking call. Supervisor calls ``wait_for_event``, tool blocks
inside plugin (polling the state file every ``poll_interval`` seconds,
default 3), returns immediately when an interesting event happens:

- ``verify`` — pipeline reached verify stage (supervisor must act)
- ``blocked`` — worker wrote BLOCKED signal
- ``checkpoint`` — BD-36 checkpoint form opened (tell user)
- ``done`` — pipeline cycle complete: stage state cleared and the cycle is
  confirmed (``done/<id>/`` archive or an awf commit) — the message names
  the TODO and the exact next command (``awf_run_next`` in a run,
  ``awf_dispatch_todo`` outside; RUN6 #1)
- ``timeout`` — no event within timeout seconds (poll again)

Token savings: ONE tool call with one response (vs N calls with N
sleep+status cycles). Supervisor doesn't burn tokens on idle polling.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ..pipeline_state import read_state
from ._helpers import require_agentic
from ._results import WaitEventResult

# B3 (run2 report): the default MCP client transport (~60s timeout) cuts a
# single wait at ~55s — the measured working cap. suggested_timeout must
# stay at or under it, or the supervisor's next call dies mid-wait (-32001).
TRANSPORT_CAP = 55
MIN_WAIT = 30

# RUN6 #1: the signs of a completed pipeline cycle. The commit comes from
# commit_gate.maybe_commit (``awf(<stage>): TODO-NNNN``), the archive from
# todos.archive_todo (``.agentic/done/<todo>/``) — either one is enough.
_TODO_ID_RE = re.compile(r"^TODO-\d{4,}$")
_AWF_COMMIT_RE = re.compile(r"^awf\([^)]+\):\s*(TODO-\d{4,})\s*$")


def _suggest_timeout(project_dir: Path, *, default: int = TRANSPORT_CAP) -> int:
    """SPEC A-run: size the next wait from measured stage durations.

    Consecutive ``Stage N/M:`` lines in orchestrator.log give the duration of
    the previous stage. Median of the last few, divided by 3 (wake ~3x per
    stage), clamped to [MIN_WAIT, TRANSPORT_CAP] seconds — never above the
    MCP transport cap, which would cut the next call mid-wait. Falls back to
    ``default`` (capped) when the log is missing or has too little history.

    AUD15-08: the stage stamps come from the shared incremental reader
    (awf/_log_reader.py) — no 5th full read of the log per wait_for_event.
    """
    from .. import paths
    from .._log_reader import read_log_snapshot

    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    stamps = read_log_snapshot(log_file).stage_stamps
    if len(stamps) < 2:
        return min(default, TRANSPORT_CAP)
    deltas = [b - a for a, b in zip(stamps, stamps[1:])]
    deltas = [d for d in deltas if 0 < d < 3600][-5:]
    if not deltas:
        return min(default, TRANSPORT_CAP)
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return max(MIN_WAIT, min(TRANSPORT_CAP, int(median / 3)))


def _cap_advice() -> str:
    """B3: the suggested wait hit the transport cap — advise smaller steps."""
    return (
        f" Transport cap: a single wait above {TRANSPORT_CAP}s is cut by the "
        "MCP client (-32001) — wait in smaller steps (suggested_timeout)."
    )


def _state_describes_live_pipeline(state: dict[str, Any] | None) -> bool:
    """True when state still carries stage markers (pipeline mid-run).

    RUN6 #1: the orchestrator's clean exit CLEARS the stage markers but
    rewrites the file with ``phase=done`` + goal/normalized (AUD02-04) —
    the file exists, ``read_state()`` is not None, yet no pipeline is
    running. That leftover used to drain a full wait timeout with a null
    snapshot (owner report 22.09, 5/5 repeats). Absent markers = exited.
    """
    if not state:
        return False
    return bool(
        state.get("stage_kind") or state.get("stage_name") or state.get("todo_id")
    )


def _last_completed_todo(project_dir: Path) -> tuple[str, str] | None:
    """Newest completed-cycle sign: ``(todo_id, kind)`` or None.

    kind is ``"commit+archive"`` (both signs, same TODO), ``"commit"``
    (newest ``awf(<stage>): TODO-NNNN`` commit) or ``"archive"`` (newest
    ``done/TODO-*/`` directory — non-git projects). The commit is the
    first sign of a finished cycle (the archive follows it in the same
    cycle), so it wins when present; the archive is the fallback.
    """
    from .. import git_utils, paths

    commit_id: str | None = None
    try:
        out = git_utils.git_stdout(
            project_dir, "log", "-n", "30", "--pretty=%s", check=False
        )
    except (RuntimeError, OSError):
        out = ""
    for line in out.splitlines():
        m = _AWF_COMMIT_RE.match(line.strip())
        if m:
            commit_id = m.group(1)
            break

    archive_id: str | None = None
    done_dir = paths.done_dir(project_dir)
    if done_dir.is_dir():
        candidates = [
            d for d in done_dir.iterdir()
            if d.is_dir() and _TODO_ID_RE.match(d.name)
        ]
        if candidates:
            newest = max(candidates, key=lambda d: d.stat().st_mtime)
            archive_id = newest.name

    if commit_id and commit_id == archive_id:
        return commit_id, "commit+archive"
    if commit_id:
        return commit_id, "commit"
    if archive_id:
        return archive_id, "archive"
    return None


def _run_is_active(project_dir: Path) -> bool:
    """SPEC A-run: is an autonomous run (забег) active in this project?"""
    from ..run_state import read_run

    run = read_run(project_dir)
    return bool(run and run.get("active"))


def _cycle_done_event(project_dir: Path) -> WaitEventResult | None:
    """RUN6 #1: the finished-cycle wake-up (the ``done`` the owner had to
    read out of ``git log`` after every approve).

    Fires ONLY when the pipeline is provably not running — no stage
    markers in state AND the process is dead (shared liveness resolver) —
    AND the project shows a completed cycle (``done/<id>/`` or an awf
    commit). Otherwise returns None and the caller keeps the old behavior
    (idle / timeout), so a live pipeline or a mid-run state can never get
    a false ``done``. The message carries the completed TODO and the
    exact next command: ``awf_run_next`` inside a run,
    ``awf_dispatch_todo`` outside it.
    """
    from ._liveness import resolve

    running, _pid, _source = resolve(project_dir)
    if running:
        return None
    evidence = _last_completed_todo(project_dir)
    if evidence is None:
        return None
    todo_id, kind = evidence

    verb = {
        "commit+archive": "committed and archived",
        "commit": "committed",
        "archive": "archived",
    }[kind]
    if _run_is_active(project_dir):
        next_step = (
            "Next step: awf_run_next(project_dir) to launch the next queued TODO."
        )
    else:
        next_step = (
            "Next step: awf_dispatch_todo(project_dir, content) for the next task."
        )

    snapshot = _state_to_dict(read_state(project_dir) or {})
    snapshot["todo_id"] = todo_id
    return WaitEventResult(
        event_type="done",
        message=f"{todo_id} {verb} — pipeline cycle complete. {next_step}",
        state_snapshot=snapshot,
    )


def wait_for_event(
    project_dir: Path,
    *,
    timeout: int = 30,
    poll_interval: int = 3,
    actionable_only: bool = False,
) -> WaitEventResult:
    """Block until pipeline event or timeout.

    Polls ``.agentic/state/current.yaml`` every ``poll_interval`` seconds.
    Returns immediately when:

    - Stage kind = ``verify`` → event_type ``verify``
    - ``last_signal`` starts with ``BLOCKED-`` → event_type ``blocked``
    - ``checkpoint_pending`` = True → event_type ``checkpoint``
    - Stage state cleared (pipeline exited; the clean-exit ``phase=done``
      leftover counts as cleared — RUN6 #1) → event_type ``done``. When the
      cycle is confirmed (``done/<id>/`` or an ``awf(<stage>): TODO-NNNN``
      commit) the message names the completed TODO and the exact next
      command — ``awf_run_next`` inside a run, ``awf_dispatch_todo``
      outside it. Without a confirmed cycle the pipeline is simply not
      running → event_type ``idle`` (no more full-timeout-on-null-state).
    - Timeout reached → event_type ``timeout``

    SPEC A-run: in a run (забег) loop pass ``timeout`` from the previous
    result's ``suggested_timeout`` (computed from measured stage durations,
    never above ``TRANSPORT_CAP`` — the MCP client transport cuts longer
    single waits, B3). The call runs in a worker thread, so long waits do
    NOT freeze other MCP tools — the old "single-thread limit" note was stale.

    Args:
        project_dir: awf project root.
        timeout: max seconds to block (default 30 — reactive mode).
        poll_interval: seconds between state checks (default 3).
        actionable_only: suppress ``stage_changed`` returns — only events
            that need supervisor action end the wait. Keeps a run loop quiet.

    Returns:
        WaitEventResult with event_type + current state snapshot.

    Raises:
        AwfApiError: if .agentic/ missing.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    suggested = _suggest_timeout(project_dir)
    deadline = time.monotonic() + timeout
    prev_state: dict[str, Any] | None = read_state(project_dir)

    # No stage markers — either the state file is absent or only the
    # clean-exit leftover (phase=done) remains (RUN6 #1). A finished cycle
    # answers 'done' immediately; with a dead process and no sign the
    # pipeline is simply not running ('idle') — the old
    # full-timeout-on-null-state drain. A LIVE process without markers is
    # mid-shutdown — the old wait behavior is kept (fall through to the
    # poll loop), no false 'done', no false 'not running'.
    if not _state_describes_live_pipeline(prev_state):
        from ._liveness import resolve

        running, _pid, _source = resolve(project_dir)
        if not running:
            done = _cycle_done_event(project_dir)
            if done:
                return done
            if not prev_state:
                return WaitEventResult(
                    event_type="idle",
                    message="No pipeline state found — pipeline not running.",
                )
            return WaitEventResult(
                event_type="idle",
                message=(
                    "Pipeline not running — stage state cleared. "
                    "Check awf_status for the project state."
                ),
            )

    # Check current state for immediate events
    result = _check_for_event(prev_state, project_dir)
    if result:
        return result

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        current_state = read_state(project_dir)

        # Stage markers gone (file cleared, or only the phase=done leftover
        # remains — RUN6 #1). A confirmed finished cycle answers with the
        # TODO + next step. Without a sign, declare "exited" only when the
        # process is actually dead — a live process is mid-shutdown (the
        # clear happens a moment before the exit), so keep polling.
        if not _state_describes_live_pipeline(current_state):
            done = _cycle_done_event(project_dir)
            if done:
                return done
            from ._liveness import resolve

            if not resolve(project_dir)[0]:
                return WaitEventResult(
                    event_type="done",
                    message="Pipeline exited (state file cleared). Check awf_report.",
                )

        # Check for actionable events FIRST (verify/blocked/checkpoint/salvage).
        # These require supervisor action and must not be masked by stage_changed.
        result = _check_for_event(current_state, project_dir)
        if result:
            return result

        # SELF-1: detect stage transitions (agent finished → next agent started).
        # Only returned when no actionable event is pending — avoids masking
        # verify/blocked events that happen to coincide with a stage transition.
        prev_stage = prev_state.get("stage_name") if prev_state else None
        curr_stage = current_state.get("stage_name")
        if prev_stage and curr_stage and prev_stage != curr_stage and not actionable_only:
            message = (
                f"Stage transition: '{prev_stage}' → '{curr_stage}'. "
                f"Previous stage completed. Poll again to wait for next event."
            )
            if suggested >= TRANSPORT_CAP:
                message += _cap_advice()
            return WaitEventResult(
                event_type="stage_changed",
                message=message,
                state_snapshot=_state_to_dict(current_state),
                suggested_timeout=suggested,
            )

        prev_state = current_state

    # Timeout
    final_state = read_state(project_dir) or {}
    # Race window: the cycle finished between the last poll and the
    # deadline — answer 'done' instead of a misleading timeout.
    if not _state_describes_live_pipeline(final_state):
        done = _cycle_done_event(project_dir)
        if done:
            return done
    message = (
        f"No event within {timeout}s. Current stage: {final_state.get('stage_name', '?')}. "
        f"Poll again."
    )
    if suggested >= TRANSPORT_CAP:
        message += _cap_advice()
    return WaitEventResult(
        event_type="timeout",
        message=message,
        state_snapshot=_state_to_dict(final_state),
        suggested_timeout=suggested,
    )


def _check_for_event(state: dict[str, Any], project_dir: Path | None = None) -> WaitEventResult | None:
    """Check if current state has an interesting event. Return result or None."""
    stage_kind = state.get("stage_kind", "")
    last_signal = state.get("last_signal", "")
    checkpoint_pending = bool(state.get("checkpoint_pending", False))
    salvage_needed = bool(state.get("salvage_needed", False))

    # DF5-4: salvage has highest priority — worker didn't signal
    if salvage_needed:
        return WaitEventResult(
            event_type="salvage",
            message=(
                f"Salvage needed: stage '{state.get('salvage_stage', '?')}' ran but "
                f"didn't produce a signal. Read .agentic/inbox/SALVAGE-*.md "
                f"for details. Review git diff, then ACK or REVIEW."
            ),
            state_snapshot=_state_to_dict(state),
        )

    if checkpoint_pending:
        return WaitEventResult(
            event_type="checkpoint",
            message=(
                "BD-36 checkpoint form open in user's browser. "
                f"Tell user to approve at: {state.get('checkpoint_form_url', '?')}"
            ),
            state_snapshot=_state_to_dict(state),
        )

    if last_signal and last_signal.startswith("BLOCKED-"):
        return WaitEventResult(
            event_type="blocked",
            message=(
                f"Worker blocked: {last_signal}. Read BLOCKED file in outbox, "
                "decide how to proceed (fix TODO, change role, or rollback)."
            ),
            state_snapshot=_state_to_dict(state),
        )

    if stage_kind == "verify":
        snapshot = _state_to_dict(state)
        # SPEC A-run (second tier): the verify callback carries the diff-stat —
        # the supervisor sees the shape of the change before opening it.
        todo_id = str(state.get("todo_id") or "")
        if project_dir and todo_id:
            try:
                from ..verify import diff_stat_for_todo

                snapshot["diff_stat"] = diff_stat_for_todo(project_dir, todo_id)
            except Exception:
                snapshot["diff_stat"] = ""
        return WaitEventResult(
            event_type="verify",
            message=(
                "Pipeline reached VERIFY stage. Read DONE reports "
                "(outbox/DONE-*.md), check git diff, verify quality. "
                "Then awf_approve or write REVIEW-*.md."
            ),
            state_snapshot=snapshot,
        )

    return None


def _state_to_dict(state: dict[str, Any]) -> dict[str, Any]:
    """Extract supervisor-relevant fields from state for the result payload."""
    return {
        "stage_name": state.get("stage_name"),
        "stage_kind": state.get("stage_kind"),
        "stage_idx": state.get("stage_idx"),
        "todo_id": state.get("todo_id"),
        "last_signal": state.get("last_signal"),
        "checkpoint_pending": state.get("checkpoint_pending", False),
        "checkpoint_form_url": state.get("checkpoint_form_url"),
        "salvage_needed": state.get("salvage_needed", False),
        "salvage_stage": state.get("salvage_stage"),
    }


__all__ = ["wait_for_event"]
