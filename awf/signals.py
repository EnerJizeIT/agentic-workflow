"""Signal handling — polling, classification, prefix filtering."""
from __future__ import annotations

import time
from pathlib import Path

from .infra import managed_fs


def signal_type(signal_filename: str) -> str:
    """Classify a signal basename (without .ready) into a type.

    AUD16-03: TEST-PASSED/TEST-FAILED are gone — no worker or stage ever
    emitted them, so they no longer classify (a stray TEST-* file is
    'unknown' and escalates like any other garbage signal).
    """
    if signal_filename.startswith("DONE-"):
        return "done"
    if signal_filename.startswith("BLOCKED-"):
        return "blocked"
    if signal_filename.startswith("REVIEW-APPROVED-"):
        return "approved"
    if signal_filename.startswith("REVIEW-REJECTED-"):
        return "rejected"
    return "unknown"


def short_id(todo_id: str) -> str:
    """Strip the 'TODO-' prefix to get the numeric part.

    Public (was _short_id). Used by both signals.py and todos.py (M2 fix —
    deduplicated, single source of truth).
    """
    return todo_id.split("-", 1)[1] if todo_id.startswith("TODO-") else todo_id


def find_signal_file(directory: Path, prefix: str, todo_id: str, suffix: str = "") -> Path | None:
    """H2/H3 fix: find a signal file in canonical OR legacy short form.

    Tries:
      - canonical:   {prefix}-{todo_id}{suffix}     (e.g. DONE-TODO-0001.md)
      - legacy short: {prefix}-{short_id}{suffix}    (e.g. DONE-0001.md)

    Returns the Path if found, None otherwise.
    """
    short = short_id(todo_id)
    for candidate_id in (todo_id, short):
        path = directory / f"{prefix}-{candidate_id}{suffix}"
        if path.exists():
            return path
    return None


def expected_signal_prefixes(kind: str) -> list[str]:
    """BD-29: return signal prefixes a stage kind is allowed to emit.

    kind is one of: "plan", "execute", "verify" (computed by pipeline.py
    from stage position). All agent stages (execute) accept the same set
    — role's skill decides what to emit.

    Args:
        kind: stage kind from Stage.kind.

    Returns:
        List of signal-name prefixes (without the trailing -TODO-NNNN.ready).
    """
    # All execute-kind stages produce the same vocabulary. The skill/role
    # decides which to actually use — awf accepts any of them.
    # AUD16-03: TEST-PASSED/TEST-FAILED removed — the full watch→classify→
    # transition cycle existed for signals nobody ever emitted.
    if kind == "execute":
        return ["DONE", "BLOCKED", "REVIEW-APPROVED", "REVIEW-REJECTED"]
    # plan and verify stages don't emit worker signals (they create TODO /
    # ACK respectively). Return empty list — caller treats as "no expected
    # signal from this stage".
    return []


def read_signal_for_todo(outbox: Path, todo_id: str, *prefixes: str) -> str | None:
    """Find the most recently written signal for a TODO among given prefixes.

    KA2-5: if multiple signals exist (e.g. DONE then BLOCKED), the one with
    the latest mtime wins — worker's final decision, not prefix priority.

    Accepts multiple filename variants:
    - canonical:   ``DONE-TODO-0001.ready``
    - legacy short: ``DONE-0001.ready``
    - agent typo:   ``DONE-TODO-0001.md.ready`` (LLMs sometimes append .ready
      to the .md filename instead of replacing .md with .ready)

    Returns basename without extension, or None.

    A signal is considered valid when the companion ``.md`` file either
    doesn't exist (``.ready``-only signals are accepted) OR exists and is
    non-empty. If .ready exists but .md is present and empty (0 bytes),
    returns None — caller treats it as "no signal yet" and the auto-DONE /
    salvage paths can still trigger.
    """
    short = short_id(todo_id)
    candidates: list[tuple[float, str]] = []  # (mtime, signal_name)

    for prefix in prefixes:
        for candidate_id in (todo_id, short):
            sig_candidates = [
                outbox / f"{prefix}-{candidate_id}.ready",
                outbox / f"{prefix}-{candidate_id}.md.ready",  # BD-21: agent typo
            ]
            for sig_file in sig_candidates:
                if not sig_file.is_file():
                    continue
                md_file = outbox / f"{prefix}-{candidate_id}.md"
                if md_file.exists():
                    try:
                        # AUD01-04: a corrupted companion (worker killed
                        # mid-write, binary garbage) must not crash the
                        # polling loop — treat unreadable as empty, which
                        # the P3 branch below already rejects.
                        md_text = md_file.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        md_text = ""
                    if not md_text.strip():
                        continue  # P3: reject whitespace-only .md (no real content)
                signal_name = sig_file.stem.rsplit(".md", 1)[0] if sig_file.stem.endswith(".md") else sig_file.stem
                mtime = sig_file.stat().st_mtime
                candidates.append((mtime, signal_name))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def stage_signal_paths(outbox: Path, todo_id: str, *prefixes: str) -> list[Path]:
    """Every signal leaf ``clean_stage_signals`` may remove — ONE enumeration.

    TODO-0235 (architect-review): a caller that removes several things in a
    row (the agent stage cleans signals AND a stale PROGRESS note) must
    preflight the whole set before the FIRST unlink. Re-spelling the
    prefix/id/extension loop at that caller would be a second implementation
    of this list — the cleanup and the preflight would drift apart.
    """
    short = short_id(todo_id)
    return [
        outbox / f"{prefix}-{candidate_id}{ext}"
        for prefix in prefixes
        for candidate_id in (todo_id, short)
        # AUD01-03: ".md.ready" (the BD-21 agent typo form) must be cleaned
        # too — read_signal_for_todo accepts it, so a clean that skipped it
        # left a VALID signal behind for the next stage.
        for ext in (".ready", ".md.ready")
    ]


def clean_stage_signals(outbox: Path, todo_id: str, *prefixes: str) -> None:
    """Remove stale signal markers this action would produce, preserving earlier-stage ones.

    NEG-4 (reports30/TODO-0190): only the signal forms are removed
    (".ready", ".md.ready"). The ".md" report is evidence, not a signal —
    read_signal_for_todo reads only .ready forms, so a surviving .md is
    inert for the next stage and stays as handoff evidence (the transition
    in pipeline_engine consumes only .ready/.md.ready for the same reason).
    A caller that must delete a stale .md does it at the call site
    (e.g. _consume_verify_decision for AUD04-04), not through this default.

    TODO-0235 (M3d, REVIEW N3): one leaf rule for destructive ops — every
    component under ``.agentic`` is checked, the LEAF included, even though
    ``unlink`` on a symlink leaf removes the link, not its target. A
    symlinked outbox surface or a symlinked signal leaf refuses with
    :class:`ManagedPathError` before the first unlink. All leaves are checked
    before the first delete, so a refusal never consumes half the set.
    """
    managed_fs.require_posix("clean stage signals")
    root = managed_fs.agentic_root_for(outbox)
    leaves = stage_signal_paths(outbox, todo_id, *prefixes)
    for p in leaves:
        managed_fs.assert_no_symlink_escape(p, root=root)
    for p in leaves:
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
