"""Supervisor wake-up: block until pipeline event (no more polling loops).

Replaces the ``while True: sleep(30); awf_status()`` pattern with a
single blocking call. Supervisor calls ``wait_for_event``, tool blocks
inside plugin (polling the state file every ``poll_interval`` seconds,
default 3), returns immediately when an interesting event happens:

- ``verify`` — pipeline reached verify stage (supervisor must act)
- ``blocked`` — worker wrote BLOCKED signal
- ``checkpoint`` — BD-36 checkpoint form opened (tell user)
- ``done`` — pipeline completed (clean exit)
- ``timeout`` — no event within timeout seconds (poll again)

Token savings: ONE tool call with one response (vs N calls with N
sleep+status cycles). Supervisor doesn't burn tokens on idle polling.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..pipeline_state import read_state
from ._helpers import require_agentic
from ._results import WaitEventResult


def _suggest_timeout(project_dir: Path, *, default: int = 180) -> int:
    """SPEC A-run: size the next wait from measured stage durations.

    Consecutive ``Stage N/M:`` lines in orchestrator.log give the duration of
    the previous stage. Median of the last few, divided by 3 (wake ~3x per
    stage), clamped to [60, 300] seconds. Falls back to ``default`` when the
    log is missing or has too little history.

    AUD15-08: the stage stamps come from the shared incremental reader
    (awf/_log_reader.py) — no 5th full read of the log per wait_for_event.
    """
    from .. import paths
    from .._log_reader import read_log_snapshot

    log_file = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    stamps = read_log_snapshot(log_file).stage_stamps
    if len(stamps) < 2:
        return default
    deltas = [b - a for a, b in zip(stamps, stamps[1:])]
    deltas = [d for d in deltas if 0 < d < 3600][-5:]
    if not deltas:
        return default
    deltas.sort()
    median = deltas[len(deltas) // 2]
    return max(60, min(300, int(median / 3)))


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
    - State file cleared (pipeline exited) → event_type ``done``
    - Timeout reached → event_type ``timeout``

    SPEC A-run: in a run (забег) loop pass ``timeout`` from the previous
    result's ``suggested_timeout`` (computed from measured stage durations).
    The call runs in a worker thread, so long waits do NOT freeze other MCP
    tools — the old "single-thread limit" note was stale.

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

    # If pipeline not running from the start → return immediately
    if not prev_state:
        return WaitEventResult(
            event_type="idle",
            message="No pipeline state found — pipeline not running.",
        )

    # Check current state for immediate events
    result = _check_for_event(prev_state, project_dir)
    if result:
        return result

    while time.monotonic() < deadline:
        time.sleep(poll_interval)
        current_state = read_state(project_dir)

        # State file cleared → pipeline exited
        if not current_state:
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
            return WaitEventResult(
                event_type="stage_changed",
                message=(
                    f"Stage transition: '{prev_stage}' → '{curr_stage}'. "
                    f"Previous stage completed. Poll again to wait for next event."
                ),
                state_snapshot=_state_to_dict(current_state),
                suggested_timeout=suggested,
            )

        prev_state = current_state

    # Timeout
    final_state = read_state(project_dir) or {}
    return WaitEventResult(
        event_type="timeout",
        message=f"No event within {timeout}s. Current stage: {final_state.get('stage_name', '?')}. Poll again.",
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
