"""Transition resolver — maps signal type to stage policy."""
from __future__ import annotations

import logging

from .pipeline import Stage

log = logging.getLogger(__name__)


def resolve_transition(stage: Stage, sig_type: str) -> tuple[str, str]:
    """Return (action, target) based on signal type and stage policy.

    Actions: next, rollback, escalate, stop, commit_and_next, commit_and_report
    Target is the stage name for rollback, empty otherwise.
    """
    if sig_type == "blocked":
        policy = stage.on_blocked
        if policy == "stop":
            return ("stop", "")
        if policy.startswith("rollback_to:"):
            return ("rollback", policy.split(":", 1)[1])
        return ("escalate", "")

    if sig_type in ("done", "approved", "passed"):
        if sig_type == "passed":
            policy = stage.on_passed
        else:
            policy = stage.on_approved
        if policy in ("commit_and_next", "commit_and_report"):
            return (policy, "")
        return ("next", "")

    if sig_type in ("rejected", "failed"):
        policy = stage.on_rejected if sig_type == "rejected" else stage.on_failed
        if policy.startswith("rollback_to:"):
            return ("rollback", policy.split(":", 1)[1])
        if policy == "replan":
            return ("escalate", "")
        return ("escalate", "")

    # Unknown signal — log loudly so user can diagnose (filename typo,
    # worker wrote unexpected signal name, etc.).
    log.warning(
        "Unknown signal type %r at stage %r — falling back to 'stop'. "
        "Expected one of: done, blocked, approved, rejected, passed, failed.",
        sig_type,
        stage.name,
    )
    return ("stop", "")
