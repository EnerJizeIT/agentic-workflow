"""RUN5 #1 (TODO-0052): leak-gate — files of a rejected attempt must not be
lost on retry.

Two real incidents bit the same way (FU-09 ``fdb3fd0`` — modules, U8b
``f0fea09`` — ``awf/data/subscriptions.json``): a TODO is rejected, its
worker-created untracked files stay in the tree, the unit is re-dispatched
(retry), the new baseline records those files as "pre-existing untracked",
and the commit gate (``awf/commit_gate.py::_files_changed_since_baseline``,
step 2 + the baseline-untracked filter) excludes them from the retry commit.
Both times the loss was repaired by a manual fix-up commit.

This module is the mechanical fix, three parts:

1. ``snapshot_rejected_files`` — on reject (``api.reject_commit`` and the
   engine's REVIEW branch) the attempt's untracked NEW files (untracked now
   minus ``BASELINE-<todo>.untracked``) are recorded to
   ``.agentic/context/REJECT-<todo>.files`` (relative paths, sorted).
   Best-effort: any failure is logged and swallowed — a broken snapshot must
   never fail the reject itself.

2. ``resolve_carry_over`` — validation for
   ``api.dispatch_todo(carry_over_from="TODO-NNNN")``: the origin TODO must
   exist (inbox or the done archive) and its REJECT file must be present.
   The dispatch then excludes those paths from the new baseline's untracked
   snapshot, so the retry's commit includes them.

3. ``orphaned_reject_files`` — the failsafe (Part B): rejected-attempt files
   that are still untracked AND still listed in the CURRENT todo's
   ``BASELINE-<todo>.untracked`` would be silently excluded from the commit.
   verify-pack and approve surface them loudly. No auto-commit, ever.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import paths
from ._errors import AwfApiError
from ._log import log as _log

_TODO_ID_RE = re.compile(r"TODO-\d{4,}")

#: git call timeout — a hung git must not hang the reject/dispatch/verify.
_GIT_TIMEOUT = 30


def reject_files_path(project_dir: Path, todo_id: str) -> Path:
    """``.agentic/context/REJECT-<todo>.files`` for a rejected attempt."""
    return paths.context_dir(Path(project_dir)) / f"REJECT-{todo_id}.files"


def _untracked_set(project_dir: Path) -> set[str] | None:
    """Current untracked (non-ignored) files, or None when git cannot say."""
    try:
        cp = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if cp.returncode != 0:
        return None
    return {ln.strip() for ln in cp.stdout.splitlines() if ln.strip()}


def _baseline_untracked_set(project_dir: Path, todo_id: str) -> set[str]:
    """Paths recorded in ``BASELINE-<todo>.untracked`` (empty when absent)."""
    f = paths.context_dir(Path(project_dir)) / f"BASELINE-{todo_id}.untracked"
    if not f.is_file():
        return set()
    try:
        return {
            ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()
        }
    except OSError:
        return set()


def snapshot_rejected_files(project_dir: Path, todo_id: str, logs_dir: Path) -> list[str]:
    """Record the attempt's untracked new files to ``REJECT-<todo>.files``.

    Returns the recorded (sorted) paths; ``[]`` when nothing is new or the
    snapshot could not be taken. Never raises — the reject must not fail on
    it (RUN5 #1 Part A.1: best-effort + log).
    """
    project_dir = Path(project_dir)
    untracked = _untracked_set(project_dir)
    if untracked is None:
        _log(logs_dir, f"leak-gate: cannot list untracked files for {todo_id} "
                       f"(not a git repo or git failed) — REJECT files not recorded")
        return []
    new = sorted(untracked - _baseline_untracked_set(project_dir, todo_id))
    try:
        reject_files_path(project_dir, todo_id).write_text(
            "".join(p + "\n" for p in new), encoding="utf-8"
        )
    except OSError as e:
        _log(logs_dir, f"leak-gate: could not write REJECT-{todo_id}.files: {e}")
        return []
    if new:
        _log(logs_dir, f"leak-gate: REJECT-{todo_id}.files — {len(new)} untracked file(s) "
                       f"of the rejected attempt recorded")
    return new


def read_reject_files(project_dir: Path, todo_id: str) -> list[str] | None:
    """Read ``REJECT-<todo>.files`` → sorted paths; None when the file is
    missing (or unreadable — treated as missing, the snapshot is
    best-effort by design)."""
    f = reject_files_path(Path(project_dir), todo_id)
    if not f.is_file():
        return None
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return sorted(ln.strip() for ln in lines if ln.strip())


def _origin_todo_exists(project_dir: Path, origin: str) -> bool:
    """The origin TODO .md: inbox (rejected TODOs stay active) or archive."""
    p = Path(project_dir)
    return (
        (paths.inbox(p) / f"{origin}.md").is_file()
        or (paths.done_dir(p) / origin / "TODO.md").is_file()
    )


def resolve_carry_over(project_dir: Path, origin: str) -> list[str]:
    """Validate a ``carry_over_from`` origin and return its recorded paths.

    Raises:
        AwfApiError: bad origin format, origin TODO not found, or
            ``REJECT-<origin>.files`` missing. No partial state on refusal —
            dispatch calls this BEFORE writing anything.
    """
    project_dir = Path(project_dir)
    if not _TODO_ID_RE.fullmatch(origin or ""):
        raise AwfApiError(
            f"invalid carry_over_from '{origin}' — expected a rejected TODO id "
            "(format TODO-NNNN)"
        )
    if not _origin_todo_exists(project_dir, origin):
        raise AwfApiError(
            f"carry_over_from '{origin}': no such TODO (checked .agentic/inbox/ and "
            ".agentic/done/). The origin must be an already-created TODO."
        )
    recorded = read_reject_files(project_dir, origin)
    if recorded is None:
        raise AwfApiError(
            f"carry_over_from '{origin}': REJECT-{origin}.files not found — the "
            "attempt was not rejected through awf reject (or the file was "
            "cleaned up). Dispatch without carry_over_from, or re-run the "
            "reject first."
        )
    return recorded


def orphaned_reject_files(
    project_dir: Path, todo_id: str
) -> list[tuple[str, list[str]]]:
    """Part B: rejected-attempt files that would be SILENTLY excluded from
    ``todo_id``'s commit.

    A path qualifies when it is (a) listed in some ``REJECT-<origin>.files``,
    (b) still untracked in the working tree, and (c) listed in
    ``BASELINE-<todo_id>.untracked`` — the exact triple the commit gate uses
    to drop it. Without ``BASELINE-<todo_id>.untracked`` the gate includes
    all untracked, so nothing is lost and nothing is reported.

    Returns ``[(origin, [paths…]), …]`` sorted by origin, paths sorted;
    ``[]`` when there is nothing to warn about (or git cannot measure).
    """
    project_dir = Path(project_dir)
    ctx = paths.context_dir(project_dir)
    baseline_listing = ctx / f"BASELINE-{todo_id}.untracked"
    if not baseline_listing.is_file():
        return []
    try:
        in_baseline = {
            ln.strip() for ln in baseline_listing.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }
    except OSError:
        return []
    if not in_baseline:
        return []
    untracked = _untracked_set(project_dir)
    if untracked is None:
        return []
    candidates = in_baseline & untracked
    if not candidates:
        return []

    result: list[tuple[str, list[str]]] = []
    if not ctx.is_dir():
        return result
    for f in sorted(ctx.glob("REJECT-*.files")):
        m = re.fullmatch(r"REJECT-(TODO-\d{4,})\.files", f.name)
        if not m:
            continue
        origin = m.group(1)
        hit = sorted(candidates & set(read_reject_files(project_dir, origin) or []))
        if hit:
            result.append((origin, hit))
    return result


__all__ = [
    "orphaned_reject_files",
    "read_reject_files",
    "reject_files_path",
    "resolve_carry_over",
    "snapshot_rejected_files",
]
