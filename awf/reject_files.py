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

from . import git_utils, paths
from ._atomic import atomic_write_text
from ._errors import AwfApiError
from ._log import log as _log
from ._path_link import decode_path_link, encode_path_link

_TODO_ID_RE = re.compile(r"TODO-\d{4,}")


def reject_files_path(project_dir: Path, todo_id: str) -> Path:
    """``.agentic/context/REJECT-<todo>.files`` for a rejected attempt."""
    return paths.context_dir(Path(project_dir)) / f"REJECT-{todo_id}.files"


def carry_over_link_path(project_dir: Path, todo_id: str) -> Path:
    """``.agentic/context/BASELINE-<todo>.carry_over`` — the leak-gate audit
    link (which REJECT origin this unit carried over), the sibling of the
    ``.include`` / ``.claim`` links."""
    return paths.context_dir(Path(project_dir)) / f"BASELINE-{todo_id}.carry_over"


def write_carry_over_link(project_dir: Path, todo_id: str, origin: str) -> None:
    """Record the carry-over origin for a re-baseline. Best-effort, the same
    contract as the include/claim links: a failed write logs but must not undo
    the dispatch (the baseline snapshot already excluded the paths)."""
    try:
        atomic_write_text(carry_over_link_path(project_dir, todo_id), f"{origin}\n")
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — carry-over link written (origin {origin})",
        )
    except OSError as e:
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — carry-over audit link not written: {e}",
        )


def read_carry_over_link(project_dir: Path, todo_id: str) -> str | None:
    """The carry-over origin recorded for a unit, or None (no carry-over /
    unreadable).

    ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``: a link
    whose bytes are not valid UTF-8 (a tampered/corrupt file) must degrade to
    None like an unreadable one — otherwise the exception escapes
    ``hygiene.transfer_exclusions`` and kills the ``awf run_next`` re-baseline
    its callers document as best-effort."""
    f = carry_over_link_path(Path(project_dir), todo_id)
    if not f.is_file():
        return None
    try:
        origin = f.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return origin or None


def clear_carry_over_link(project_dir: Path, todo_id: str) -> None:
    """Drop a stale carry-over link: a (re-)dispatch without ``carry_over_from``
    rewrites the baseline WITHOUT the exclusion, so a later re-baseline must
    not re-apply a carry-over that no longer holds — the same lifecycle as the
    include and claim links."""
    carry_over_link_path(Path(project_dir), todo_id).unlink(missing_ok=True)


def _raw_untracked(project_dir: Path) -> list[str] | None:
    """Current untracked (non-ignored) paths as RAW display strings, or None.

    Read through the R2.1 model (``ls-files --others --exclude-standard -z``,
    byte protocol): the printed listing C-quotes a non-ASCII name, so the
    leak-gate used to store a form the R2.2 ownership model (raw bytes) never
    matched — a non-ASCII finding stayed in ``BASELINE-*.untracked`` (the
    commit gate then dropped it) and was refused under ``require_clean``
    (qa iter 4). ``None`` when git cannot say.
    """
    from .infra.git_model import GitModelError, GitRepository, display_path

    try:
        return [display_path(p) for p in GitRepository(project_dir).untracked_paths()]
    except (GitModelError, OSError):
        return None


def _untracked_view(project_dir: Path) -> tuple[list[str], dict[str, str]] | None:
    """The current untracked listing as ``(raw_paths, raw -> printed)``, None
    when git cannot say.

    The leak-gate speaks the same RAW identity as every other transfer channel
    (``include_untracked`` / ``claim_from``), while
    ``BASELINE-<todo>.untracked`` keeps git's PRINTED lines
    (``pipeline._untracked_snapshot`` bridges them positionally — R2.3 migrates
    both sides together). Both listings come from the SAME deterministic
    ``git ls-files`` scan in the SAME byte order, so index i is one file; the
    printed form of a raw path is known without re-parsing git's C-quoting (the
    one-parser principle). When the two listings do not pair (a newline in a
    name splits the printed one) the map degrades to identity — the legacy
    behaviour, never worse.
    """
    raw = _raw_untracked(project_dir)
    if raw is None:
        return None
    try:
        # Second git read (the PRINTED listing). It must carry the SAME
        # best-effort guard as the raw read: ``git_utils._git(text=True)``
        # lets ``subprocess.TimeoutExpired`` / ``OSError`` (hung or missing
        # git) and ``UnicodeDecodeError`` (non-UTF-8 name under
        # ``core.quotepath=false``) escape. Without it a transient git
        # failure would raise out of ``snapshot_rejected_files`` (contract:
        # never raises) and ``orphaned_reject_files`` (contract: ``[]`` when
        # git cannot measure) — the failsafe surfaces of verify-pack/approve.
        printed = git_utils.untracked_files(project_dir)
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        return None
    if len(printed) != len(raw):
        return raw, {p: p for p in raw}
    return raw, dict(zip(raw, printed))


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
    view = _untracked_view(project_dir)
    if view is None:
        _log(logs_dir, f"leak-gate: cannot list untracked files for {todo_id} "
                       f"(not a git repo or git failed) — REJECT files not recorded")
        return []
    raw_paths, raw_to_printed = view
    baseline = _baseline_untracked_set(project_dir, todo_id)
    new = sorted(
        p for p in raw_paths if raw_to_printed.get(p, p) not in baseline
    )
    try:
        # The paths are RAW (``display_path``: ``surrogateescape``); the shared
        # codec writes each entry NUL-terminated, so a name with a newline /
        # control byte / edge space (and a non-UTF-8 surrogate) round-trips
        # byte-faithfully. Strict UTF-8 would raise ``UnicodeEncodeError`` — a
        # ``ValueError``, NOT the ``OSError`` this guard swallows — and break
        # the "never raises" contract by failing the reject itself.
        reject_files_path(project_dir, todo_id).write_text(
            encode_path_link(new),
            encoding="utf-8",
            errors="surrogateescape",
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
        # Same codec as the writer (:mod:`awf._path_link`): the NUL form keeps
        # the raw identity of a name with a newline / control byte / edge space
        # / non-UTF-8 surrogate; binary content degrades to a missing link.
        return decode_path_link(f.read_bytes())
    except OSError:
        return None


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
    view = _untracked_view(project_dir)
    if view is None:
        return []
    raw_paths, raw_to_printed = view
    # A path is an orphan candidate when it is STILL untracked (present in the
    # current raw listing) AND its printed form sits in
    # ``BASELINE-<todo>.untracked`` — the exact triple the commit gate uses to
    # drop it. The baseline comparison stays in the file's printed form, reached
    # through the raw->printed map (one bridge, both sides agree on the file).
    candidates = {
        p for p in raw_paths if raw_to_printed.get(p, p) in in_baseline
    }
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
    "carry_over_link_path",
    "clear_carry_over_link",
    "orphaned_reject_files",
    "read_carry_over_link",
    "read_reject_files",
    "reject_files_path",
    "resolve_carry_over",
    "snapshot_rejected_files",
    "write_carry_over_link",
]
