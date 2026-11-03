"""Transition resolver — maps signal type to stage policy."""
from __future__ import annotations

import logging

from .pipeline import Stage, policy_target

log = logging.getLogger(__name__)


def resolve_transition(stage: Stage, sig_type: str) -> tuple[str, str]:
    """Return (action, target) based on signal type and stage policy.

    Actions: next, rollback, escalate, fix_round, stop, commit_and_next,
    commit_and_report. Target is the stage name for rollback/fix_round,
    empty otherwise.
    """
    if sig_type == "blocked":
        policy = stage.on_blocked
        if policy == "stop":
            return ("stop", "")
        if policy.startswith("rollback_to:"):
            return ("rollback", policy_target(policy))
        return ("escalate", "")

    if sig_type in ("done", "approved"):
        policy = stage.on_approved
        if policy in ("commit_and_next", "commit_and_report"):
            return (policy, "")
        return ("next", "")

    if sig_type == "rejected":
        policy = stage.on_rejected
        # A7: `fix_round:<stage>` rewinds to the fixing stage AND appends the
        # findings to the unit — the reviewer's verdict becomes the worker's
        # next instruction instead of a supervisor replan.
        if policy.startswith("fix_round:"):
            return ("fix_round", policy_target(policy))
        if policy.startswith("rollback_to:"):
            return ("rollback", policy_target(policy))
        # A32: 'replan' is the ONLY value that grows the queue — it names
        # the replan subprocess (refined TODO). 'escalate' (default) and
        # the two prefixed forms above are resolved by the engine as a
        # rewind of the SAME unit: the report is appended and the stage
        # returns to its author. The resolver returns the same "escalate"
        # action for both words on purpose — the engine, not the mapping,
        # owns "does a refusal create a unit" (awf/rejection.py).
        return ("escalate", "")

    # Unknown signal — treat as blocked so supervisor gets salvage context
    # (P3: was "stop" which left no recovery path for the supervisor).
    # AUD16-03: "passed"/"failed" land here too — TEST-PASSED/TEST-FAILED
    # no longer exist, so the only real types are the ones below.
    msg = (
        f"Unknown signal type {sig_type!r} at stage {stage.name!r} — treating as blocked. "
        f"Expected one of: done, blocked, approved, rejected."
    )
    import sys
    print(f"WARNING: {msg}", file=sys.stderr)
    log.warning("%s", msg)
    # Escalate to supervisor — same as blocked, gives recovery path
    return ("escalate", "")
