"""Supervisor wake-up: block until pipeline event (no more polling loops).

Replaces the ``while True: sleep(30); awf_status()`` pattern with a
single blocking call. Supervisor calls ``wait_for_event``, tool blocks
inside plugin (polling state file every 10s), returns immediately when
an interesting event happens:

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


def wait_for_event(
    project_dir: Path,
    *,
    timeout: int = 30,
    poll_interval: int = 10,
) -> WaitEventResult:
    """Block until pipeline event or timeout.

    Polls ``.agentic/state/current.yaml`` every ``poll_interval`` seconds.
    Returns immediately when:

    - Stage kind = ``verify`` → event_type ``verify``
    - ``last_signal`` starts with ``BLOCKED-`` → event_type ``blocked``
    - ``checkpoint_pending`` = True → event_type ``checkpoint``
    - State file cleared (pipeline exited) → event_type ``done``
    - Timeout reached → event_type ``timeout``

    Args:
        project_dir: awf project root.
        timeout: max seconds to block (default 30 — under MCP plugin
            single-thread limit; longer values freeze ALL other MCP tools).
        poll_interval: seconds between state checks (default 10).

    Returns:
        WaitEventResult with event_type + current state snapshot.

    Raises:
        AwfApiError: if .agentic/ missing.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    deadline = time.monotonic() + timeout
    prev_state: dict[str, Any] | None = read_state(project_dir)

    # If pipeline not running from the start → return immediately
    if not prev_state:
        return WaitEventResult(
            event_type="idle",
            message="No pipeline state found — pipeline not running.",
        )

    # Check current state for immediate events
    result = _check_for_event(prev_state)
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

        # Check for events
        result = _check_for_event(current_state)
        if result:
            return result

        prev_state = current_state

    # Timeout
    final_state = read_state(project_dir) or {}
    return WaitEventResult(
        event_type="timeout",
        message=f"No event within {timeout}s. Current stage: {final_state.get('stage_name', '?')}. Poll again.",
        state_snapshot=_state_to_dict(final_state),
    )


def _check_for_event(state: dict[str, Any]) -> WaitEventResult | None:
    """Check if current state has an interesting event. Return result or None."""
    stage_kind = state.get("stage_kind", "")
    last_signal = state.get("last_signal", "")
    checkpoint_pending = bool(state.get("checkpoint_pending", False))

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
        return WaitEventResult(
            event_type="verify",
            message=(
                "Pipeline reached VERIFY stage. Read DONE reports "
                "(outbox/DONE-*.md), check git diff, verify quality. "
                "Then awf_approve or write REVIEW-*.md."
            ),
            state_snapshot=_state_to_dict(state),
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
    }


__all__ = ["wait_for_event"]
