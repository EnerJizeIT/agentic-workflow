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
   engine's REVIEW branch) the attempt's files are recorded to
   ``.agentic/context/REJECT-<todo>.files`` (relative paths, sorted):
   the untracked NEW files (untracked now minus
   ``BASELINE-<todo>.untracked``) plus the tracked paths the attempt
   changed (current changes minus the baseline's dirty set — the
   pre-dispatch work). Best-effort: any failure is logged and swallowed —
   a broken snapshot must never fail the reject itself.

2. ``resolve_carry_over`` — validation for
   ``api.dispatch_todo(carry_over_from="TODO-NNNN")``: the origin TODO must
   exist (inbox or the done archive) and its REJECT file must be present.
   The dispatch then excludes those paths from the new baseline's untracked
   snapshot and the commit plan adopts them (carry-over tracked paths are
   owned by the retry), so the retry's candidate and commit include them.

3. ``orphaned_reject_files`` — the failsafe (Part B): rejected-attempt files
   that are still untracked AND still listed in the CURRENT todo's
   ``BASELINE-<todo>.untracked`` would be silently excluded from the commit.
   verify-pack and approve surface them loudly. No auto-commit, ever.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paths
from ._errors import AwfApiError
from ._log import log as _log
from ._path_link import decode_path_link, encode_path_link
from .infra import managed_fs

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
        target = carry_over_link_path(project_dir, todo_id)
        managed_fs.managed_write_under_agentic(target, f"{origin}\n")
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — carry-over link written (origin {origin})",
        )
    except (OSError, managed_fs.ManagedPathError) as e:
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


def _baseline_untracked_set(project_dir: Path, todo_id: str) -> set[str]:
    """Paths recorded in ``BASELINE-<todo>.untracked``, RAW (empty when absent).

    One reader for the fact: the raw NUL form and the legacy printed form are
    both decoded to the model's raw identity, so the comparison below needs no
    printed bridge of its own.
    """
    from .infra.git_model import read_untracked_snapshot

    return read_untracked_snapshot(project_dir, todo_id) or set()


def _adopted_paths(project_dir: Path, todo_id: str, logs_dir: Path) -> set[str]:
    """The paths the unit OWNS although its baseline carried them dirty.

    The transfer channels it adopted (carry-over / include / claim), resolved
    through the ONE resolution home (:func:`api.hygiene.transfer_exclusions`)
    so this reader cannot drift from the baseline and the commit plan.

    Best-effort, like the snapshot itself: a link-read failure returns the
    empty set (the pre-existing behaviour — the paths stay foreign) and logs.
    The reject must never fail on it.
    """
    try:
        from .api.hygiene import transfer_exclusions

        carry_over, include, claim = transfer_exclusions(project_dir, todo_id)
    except Exception as exc:
        _log(
            logs_dir,
            f"leak-gate: cannot resolve the adopted paths of {todo_id} "
            f"({type(exc).__name__}: {exc}) — they keep counting as foreign",
        )
        return set()
    return set(carry_over) | set(include) | set(claim)


def _tracked_attempt_paths(
    project_dir: Path, todo_id: str, logs_dir: Path
) -> list[str]:
    """Tracked paths the attempt changed — current changes minus the
    baseline's dirty set (the pre-dispatch work).

    A rejected attempt's tracked edit stays in the worktree; the retry's
    baseline (taken on that dirty tree) would record it as pre-existing and
    the delta mechanism would drop it from the retry commit. Recording the
    path here lets ``carry_over_from`` adopt it. Untracked paths keep their
    own snapshot (``_raw_untracked``) and are NOT listed by this helper.

    Only a RENAME owns both of its paths: a copy's source comes from a
    similarity heuristic, never from the baseline facts, so it is not the
    attempt's footprint (the same rule as
    ``git_model.operations_from_changes``).

    Best-effort: a git failure returns ``[]`` and logs — the reject must not
    fail, but a silent loss of the tracked delta is the very leak this module
    removes.

    An UNKNOWN baseline (no ``BASELINE-<todo>.status`` — a legacy/ad-hoc
    unit) is not the same fact as a clean one: without the dirty set every
    current tracked change looks like the attempt's, so a foreign pre-dispatch
    edit would be recorded and then committed by the retry (CTX1 audit 0259).
    Distinguish ``None`` from the empty set: unknown baseline records NO
    tracked delta, with a log.

    A path the unit ADOPTED through a transfer channel (carry-over / include /
    claim) is its own work even though the baseline carried it dirty — a
    retry's baseline is taken on the rejected attempt's tree, so the
    predecessor's paths sit in the retry's own dirty set. They stay in the
    delta: without that a SECOND reject loses them and the next retry drops
    them from its commit (the A → B → C chain).

    The baseline's UNTRACKED snapshot is the second foreign source
    (``DeltaAssembler._path_owned``): a pre-existing untracked file the
    attempt staged is foreign — absent snapshot keeps the recorded rule.
    """
    from .commit_delta import baseline_dirty_paths
    from .infra.git_model import GitModelError, GitRepository, display_path, read_untracked_snapshot

    pre_existing = baseline_dirty_paths(project_dir, todo_id)
    if pre_existing is None:
        _log(
            logs_dir,
            f"leak-gate: BASELINE-{todo_id}.status missing — pre-dispatch "
            "tracked changes unknown, tracked delta not recorded",
        )
        return []
    adopted = _adopted_paths(project_dir, todo_id, logs_dir)
    pre_existing -= adopted
    pre_existing_untracked = read_untracked_snapshot(project_dir, todo_id)
    if pre_existing_untracked is not None:
        pre_existing_untracked -= adopted
    try:
        changes = GitRepository(project_dir).changes()
    except (GitModelError, OSError) as exc:
        _log(
            logs_dir,
            f"leak-gate: cannot list tracked changes for {todo_id} "
            f"({type(exc).__name__}: {exc}) — tracked delta not recorded",
        )
        return []
    tracked: set[str] = set()
    for record in changes:
        if record.kind == "untracked":
            continue
        raw_sides = (
            (record.path, record.old_path)
            if record.kind == "renamed"
            else (record.path,)
        )
        for raw in raw_sides:
            if raw is None:
                continue
            shown = display_path(raw)
            foreign = shown in pre_existing or (
                pre_existing_untracked is not None
                and shown in pre_existing_untracked
            )
            if not foreign:
                tracked.add(shown)
    return sorted(tracked)


def snapshot_rejected_files(project_dir: Path, todo_id: str, logs_dir: Path) -> list[str]:
    """Record the attempt's files to ``REJECT-<todo>.files``.

    Entries are the untracked new files (current untracked minus
    ``BASELINE-<todo>.untracked``) and the tracked paths the attempt
    changed (current changes minus the baseline's dirty set) — the
    requirement's "only the attempt's paths, not the whole dirty
    worktree".

    Returns the recorded (sorted) paths; ``[]`` when nothing is new or the
    snapshot could not be taken. Never raises — the reject must not fail on
    it (RUN5 #1 Part A.1: best-effort + log).
    """
    project_dir = Path(project_dir)
    raw_paths = _raw_untracked(project_dir)
    if raw_paths is None:
        _log(logs_dir, f"leak-gate: cannot list untracked files for {todo_id} "
                       f"(not a git repo or git failed) — REJECT files not recorded")
        return []
    baseline = _baseline_untracked_set(project_dir, todo_id)
    new = set(raw_paths) - baseline
    new.update(_tracked_attempt_paths(project_dir, todo_id, logs_dir))
    new = sorted(new)
    try:
        # The paths are RAW (``display_path``: ``surrogateescape``); the shared
        # codec writes each entry NUL-terminated, so a name with a newline /
        # control byte / edge space (and a non-UTF-8 surrogate) round-trips
        # byte-faithfully. Strict UTF-8 would raise ``UnicodeEncodeError`` — a
        # ``ValueError``, NOT the ``OSError`` this guard swallows — and break
        # the "never raises" contract by failing the reject itself.
        target = reject_files_path(project_dir, todo_id)
        managed_fs.managed_write_under_agentic(
            target, encode_path_link(new), errors="surrogateescape"
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(logs_dir, f"leak-gate: could not write REJECT-{todo_id}.files: {e}")
        return []
    if new:
        _log(logs_dir, f"leak-gate: REJECT-{todo_id}.files — {len(new)} file(s) "
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
    in_baseline = _baseline_untracked_set(project_dir, todo_id)
    if not in_baseline:
        return []
    raw_paths = _raw_untracked(project_dir)
    if raw_paths is None:
        return []
    # A path is an orphan candidate when it is STILL untracked (present in the
    # current raw listing) AND sits in ``BASELINE-<todo>.untracked`` — the
    # exact triple the commit gate uses to drop it. Both sides are the model's
    # raw identity, so no printed bridge is needed.
    candidates = {p for p in raw_paths if p in in_baseline}
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
