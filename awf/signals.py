"""Signal handling — polling, classification, prefix filtering."""
from __future__ import annotations

import time
from pathlib import Path


def signal_type(signal_filename: str) -> str:
    """Classify a signal basename (without .ready) into a type."""
    if signal_filename.startswith("DONE-"):
        return "done"
    if signal_filename.startswith("BLOCKED-"):
        return "blocked"
    if signal_filename.startswith("REVIEW-APPROVED-"):
        return "approved"
    if signal_filename.startswith("REVIEW-REJECTED-"):
        return "rejected"
    if signal_filename.startswith("TEST-PASSED-"):
        return "passed"
    if signal_filename.startswith("TEST-FAILED-"):
        return "failed"
    return "unknown"


def _short_id(todo_id: str) -> str:
    """Strip the 'TODO-' prefix to get the numeric part."""
    return todo_id.split("-", 1)[1] if todo_id.startswith("TODO-") else todo_id


def expected_signal_prefixes(action: str) -> list[str]:
    """Return signal prefixes an action is allowed to emit."""
    mapping = {
        "execute_todo": ["DONE", "BLOCKED"],
        "review_code": ["REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED"],
        "audit_code": ["REVIEW-APPROVED", "REVIEW-REJECTED", "BLOCKED"],
        "run_tests": ["TEST-PASSED", "TEST-FAILED", "BLOCKED"],
    }
    return mapping.get(action, [
        "DONE", "BLOCKED", "REVIEW-APPROVED", "REVIEW-REJECTED",
        "TEST-PASSED", "TEST-FAILED",
    ])


def read_signal_for_todo(outbox: Path, todo_id: str, *prefixes: str) -> str | None:
    """Find the first matching signal for a TODO among given prefixes.

    Accepts both canonical (DONE-TODO-0001) and legacy short (DONE-0001) forms.
    Returns basename without .ready, or None.

    A signal is considered valid when the companion ``.md`` file either
    doesn't exist (``.ready``-only signals are accepted) OR exists and is
    non-empty. If .ready exists but .md is present and empty (0 bytes),
    returns None — caller treats it as "no signal yet" and the auto-DONE /
    salvage paths can still trigger.
    """
    short = _short_id(todo_id)
    for prefix in prefixes:
        for candidate_id in (todo_id, short):
            sig_file = outbox / f"{prefix}-{candidate_id}.ready"
            if not sig_file.is_file():
                continue
            # Require companion .md (non-empty) for primary closure signals.
            # PROGRESS-* and DONE-* typically carry .md reports. Some signals
            # (e.g. APPROVE written by humans via `awf approve`) are .ready-only
            # by design — those are inbox/ signals, not outbox/, so this check
            # doesn't affect them.
            md_file = outbox / f"{prefix}-{candidate_id}.md"
            if md_file.exists() and md_file.stat().st_size == 0:
                # .ready exists but .md is empty — treat as not-ready.
                continue
            return sig_file.stem
    return None


def clean_stage_signals(outbox: Path, todo_id: str, *prefixes: str) -> None:
    """Remove stale signals this action would produce, preserving earlier-stage ones."""
    short = _short_id(todo_id)
    for prefix in prefixes:
        for candidate_id in (todo_id, short):
            for ext in (".ready", ".md"):
                p = outbox / f"{prefix}-{candidate_id}{ext}"
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass


def wait_for_signal(
    outbox: Path,
    todo_id: str,
    *prefixes: str,
    timeout: int = 3600,
    interval: int = 2,
) -> str:
    """Poll for a signal file. Raises TimeoutError on expiry."""
    if not prefixes:
        prefixes = ("DONE", "BLOCKED")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sig = read_signal_for_todo(outbox, todo_id, *prefixes)
        if sig:
            return sig
        time.sleep(interval)

    raise TimeoutError(f"Stage timed out after {timeout}s waiting for signal")
