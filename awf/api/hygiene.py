"""TODO state hygiene — stale-closure clearing and never-started removal.

RUN3 #4/#5: after an interrupted run a stale ``outbox/BLOCKED-<id>.ready``
keeps ``todos.is_closed`` true, so a re-issued TODO is invisible to
``awf_status`` / ``awf_start``. These helpers make the two repairs explicit
operations that leave a trace:

- :func:`unblock_todo` moves BLOCKED/ACK closure signals (canonical and
  legacy forms) into a timestamped ``context/`` directory. DONE signals are
  never touched — an archived TODO comes back only via ``awf restore``.
- :func:`remove_todo` deletes a TODO that never started (no PROGRESS, no
  signals; the dispatch ``.ready`` is an artifact and moves to the trace
  dir); ``context/BASELINE-<id>.*`` is deleted with the unit.
- :func:`retire_todo` archives a rejected/abandoned ACTIVE TODO (battle
  case TODO-0035: reject leaves ``DONE-<id>.{md,json}`` without the
  ``.ready`` signal, so the TODO stays "active" forever) — the files move
  to ``done/<id>/`` with a ``RETIRED-<ts>.md`` note; no DONE signal is
  written, and ``awf restore`` still works.
- :func:`update_todo` rewords a not-started TODO in place, keeping the
  number, ``.ready`` and the baseline (previous content backed up to
  ``context/TODO-<id>.md.bak-<ts>``); started TODOs are refused.
- :func:`review_append` appends a numbered supervisor
  ``## REVIEW-фикс (супервизор, виток N)`` section to a STARTED TODO (the
  inverse of :func:`update_todo`), with the same backup trace.
- :func:`blocked_draft` builds a ready ``## REVIEW-фикс`` text from a
  standard ``outbox/BLOCKED-<id>.md`` note — read-only, the supervisor
  pastes it into the TODO and fills the decision slots.
- :func:`clear_stale_closures` is the shared helper — ``dispatch_todo``
  calls it automatically on re-dispatch of the same number.
"""
from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from .. import paths, run_state, todos
from .._atomic import MODE_OWNER_EDITABLE
from .._errors import OperationRecoveryRequired
from .._log import log as _log
from .._markdown import append_block, section_body
from .._names import unique_name
from ..checklists import (
    CHECKLIST_NAME,
    ChecklistError,
    block_has_items,
    block_of,
    validate_readable_block,
)
from ..infra import managed_fs
from ..pipeline_state import read_state
from ..signals import short_id
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import (
    managed_mkdir_api,
    managed_write_api_text,
    refuse_unsafe_surfaces,
    require_agentic,
)
from ._results import (
    BlockedDraftResult,
    RemoveTodoResult,
    RetireTodoResult,
    ReviewAppendResult,
    UnblockResult,
    UpdateTodoResult,
)

_ID_RE = re.compile(r"^TODO-\d{4,}$")
#: R1.2 (TODO-0216): an OperationId is a portable, path-safe token naming one
#: durable operation (retry/replay key). Deliberately narrow — it becomes a
#: filename under state/operations/.
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_id(todo_id: str) -> None:
    if not _ID_RE.match(todo_id or ""):
        raise AwfApiError(f"invalid todo_id {todo_id!r}, expected TODO-NNNN")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


# ─── R1.2 (TODO-0216): operation identity + guarded read-modify-write ─────
#
# The MCP mutation lock (agent_workflow_ui awf.py) serializes MCP calls but
# its lifetime is the coroutine's, not the writing thread's: a cancelled
# call released the lock while its worker kept writing (audit S5-01), and
# CLI/HTTP callers never held it at all. The integrity guarantee therefore
# lives here, below the API layer:
#
# * `_write_todo_checked` writes the TODO under the project file lock and
#   only if its content is still the exact text this call read
#   (compare-and-swap on the text) — a stale write is refused, never
#   written over a newer accepted one;
# * a durable operation journal (one json per OperationId) lets a retry of
#   a cancelled/hung call read the actual outcome instead of guessing:
#   applied → replay the result, refused → same error, running-without-
#   finish → recovery_required.


def _write_todo_locked(md: Path, expected_text: str, new_text: str) -> None:
    """R1.2: the version-guarded write; the caller MUST hold the project
    lock (see :func:`_write_todo_checked`). Split out so an outer lock
    holder (the durable-operation path) does not re-lock and deadlock.

    No ``project_dir`` argument (REVIEW architect): the barrier derives the
    root from the leaf, so a second spelling of the same root would only
    add a way for the two to disagree (the C4/Q4 class)."""
    # W3: the TODO is a byte-faithful input (the same contract as the
    # T1 readers) — a non-UTF-8 file must survive a version check, an
    # append and a backup, so the read and BOTH writes stay in the
    # surrogateescape spelling.
    current = md.read_text(encoding="utf-8", errors="surrogateescape")
    if current != expected_text:
        raise AwfApiError(
            f"{md.name} changed after it was read — write refused "
            "(version conflict: a concurrent update landed first). "
            "Re-read the TODO and retry; the accepted update is kept."
        )
    managed_write_api_text(
        md,
        new_text,
        operation="update todo",
        mode=MODE_OWNER_EDITABLE,
        errors="surrogateescape",
    )


def _write_todo_checked(
    project_dir: Path,
    md: Path,
    expected_text: str,
    new_text: str,
    *,
    _lock_held: bool = False,
) -> None:
    """R1.2 (TODO-0216): the read-modify-write write step, guarded.

    Holds the project file lock across re-read + compare + write, so the
    decision "is my version still current" and the write itself cannot
    interleave with a concurrent writer. If the on-disk text no longer
    matches what this call read (``expected_text``), the write is refused
    with a clear error — the newer accepted update is kept.

    ``_lock_held``: set by the durable-operation path, which already holds
    the project lock for the whole journal+apply section — re-locking here
    would deadlock (flock is per open file description).
    """
    if _lock_held:
        _write_todo_locked(md, expected_text, new_text)
        return
    from .._lock import locked

    with locked(project_dir):
        _write_todo_locked(md, expected_text, new_text)


def _validate_operation_id(operation_id: str) -> None:
    if not _OPERATION_ID_RE.match(operation_id or ""):
        raise AwfApiError(
            f"invalid operation_id {operation_id!r} — letters, digits, '.', "
            "'_', '-' only (1..64 chars, must start alphanumeric)"
        )


def _read_operation(project_dir: Path, operation_id: str) -> dict | None:
    """R1.2: the recorded outcome for ``operation_id``, or
    ``None`` when the operation was never started.

    An unreadable or corrupt record is NOT "never started": the attempt
    left a file whose content cannot be trusted, so the outcome is
    unknown. Returning ``None`` there would let a retry re-apply the
    effect — the duplicate requirement 4 forbids — so the unknown outcome
    is surfaced as :class:`OperationRecoveryRequired` instead.

    TODO-0227 (M3): reading goes through the ONE journal schema owner
    (``state_store.read_operation``): the legacy hygiene ``started`` line is
    read as ``running`` (dual read), and a foreign format is
    recovery_required — never a second parser next to the Store's.
    """
    from ..infra import state_store

    return state_store.read_operation(project_dir, operation_id)


def _write_operation(project_dir: Path, operation_id: str, **fields) -> None:
    """R1.2 (TODO-0216, F-HIGH): persist one journal transition.

    Raises on failure — deliberately. The journal IS the durable
    guarantee, not a log line: a swallowed OSError would leave either no
    ``running`` record (a retry reads "never started" and re-applies the
    effect) or an unfinishable ``running`` record while the effect landed.
    The caller decides what a failure means: the START write refuses
    BEFORE any effect, a FINISH write failure surfaces
    ``recovery_required``.

    TODO-0227 (M3): the record body (status vocabulary, ``ts`` format,
    ``args_digest``, bytes) is built by the one schema module through the
    Store writer — hygiene no longer spells its own timestamp or status.
    """
    from ..infra import state_store

    state_store.write_operation(project_dir, operation_id, **fields)


def _durable_operation(
    project_dir: Path,
    operation_id: str,
    todo_id: str,
    action: str,
    *,
    result_keys: frozenset[str],
    replay: Callable[[dict], Any],
    apply: Callable[[Path], Any],
    args: Any = None,
) -> Any:
    """The R1.2 durable-operation protocol — ONE implementation.

    Both TODO writers (``update_todo`` and ``review_append``) promise the
    same retry contract (TODO-0240 review: the second copy was written
    next to the first and could drift, as the CLI already did):

    - a recorded ``applied`` replay returns the stored result — no second
      backup, no second section;
    - a recorded ``refused`` replay does NOT stick: the same id
      re-runs the operation (the refusal was a snapshot of the
      preconditions, not a verdict);
    - a ``running`` record with no finish means the outcome is unknown —
      :class:`OperationRecoveryRequired` is raised, never a duplicate
      effect.

    ``apply`` receives the resolved project dir and runs with the lock
    held (``flock`` is per open file description: nesting deadlocks).
    ``result_keys`` is the minimum the replay needs to rebuild the result;
    a recorded ``applied`` with an incomplete body is not a factual
    outcome, it is recovery_required. A refusal (an expected
    ``AwfApiError``) is recorded as ``refused``; any other exception
    leaves the record at ``running`` on purpose — a crash mid-write is
    exactly the unknown outcome the retry must detect.

    ``args`` (AA1, TODO-0240 review v7) is the call's content payload —
    the text a writer would append. Its digest is journalled with the
    record: the same ``operation_id`` with a DIFFERENT payload is not a
    retry of the recorded operation, it is a second operation wearing the
    same name, so it is refused as recovery_required instead of silently
    replaying the first outcome. A record written before the digest
    existed (``None``) cannot be compared and stays replayable. The
    argument is OPTIONAL: a writer whose documented contract is
    "the id alone names the operation" (``update_todo``) passes none and
    keeps that contract.
    """
    _validate_id(todo_id)
    _validate_operation_id(operation_id)
    resolved = Path(project_dir).resolve()
    require_agentic(resolved)
    from .._lock import locked
    from ..infra import state_store

    args_digest = None if args is None else state_store.payload_digest(args)

    with locked(resolved):
        prior = _read_operation(resolved, operation_id)
        if prior is not None:
            # AUD-1 (TODO-0216 iter-4): an operation_id names ONE operation.
            # Reusing it for another unit or action must not hand back the
            # first operation's result — the journal records the target it
            # actually effected; anything else is an unknown outcome, not a
            # foreign ok.
            recorded_target = (prior.get("todo_id"), prior.get("action"))
            if recorded_target != (todo_id, action):
                raise OperationRecoveryRequired(
                    operation_id,
                    f"operation {operation_id}: replay requested for "
                    f"({todo_id}, {action}) but the journal records "
                    f"{recorded_target} — the id names a different operation; "
                    "outcome unknown, recovery_required, do not retry blindly",
                )
            recorded_args = prior.get("args_digest")
            if (
                args_digest is not None
                and recorded_args is not None
                and recorded_args != args_digest
            ):
                # AA1 (TODO-0240 review v7): one key — one payload. A
                # repeat with a different text is not the recorded
                # operation; replaying the first result would hide the
                # second request and its effect would stay unwritten.
                raise OperationRecoveryRequired(
                    operation_id,
                    f"operation {operation_id}: the key was reused with a "
                    "different payload (the journal records other content) "
                    "— not a retry; recovery_required, do not retry blindly",
                )
            status = prior.get("status")
            if status == "applied":
                stored = prior.get("result")
                if not isinstance(stored, dict) or not result_keys <= stored.keys():
                    raise OperationRecoveryRequired(
                        operation_id,
                        f"operation {operation_id}: journal records 'applied' "
                        "but carries no complete result — outcome unknown, "
                        "recovery_required, do not retry blindly",
                    )
                return replay(stored)
            if status != "refused":
                raise OperationRecoveryRequired(operation_id)
            # TODO-0242 (B.8): a refusal is a snapshot of the preconditions
            # at that moment, not a verdict forever. Fall through to the
            # standard start record below: it overwrites the refusal with a
            # fresh ``running``, and the apply re-checks the world. The old
            # replay raised the stored error even after the cause (a live
            # pipeline, a stale lock) was gone, so it stuck to the id.
        # F-HIGH: the START record must land BEFORE any effect. If it
        # cannot, refuse now — a retry with no record would re-apply.
        try:
            _write_operation(
                resolved, operation_id, status="running", todo_id=todo_id,
                action=action, args_digest=args_digest,
            )
        except state_store.STATE_WRITE_FAILURES as e:
            # F-AR1 (architect-review): the writer runs under the managed
            # barrier, whose refusal is ``ManagedPathError`` — NOT an
            # OSError. Catch the ONE state-write family, not a respelled
            # subset: the documented "refused before any effect, retry is
            # safe" contract must hold for a swapped/symlinked
            # ``.agentic``/``state`` too.
            raise AwfApiError(
                f"operation {operation_id}: journal start write failed "
                f"({e}) — refused before any effect; retry is safe"
            ) from e
        try:
            result = apply(resolved)
        except AwfApiError as e:
            message = (
                f"{e} (the refusal is not final — after the cause is "
                "removed, retry with the same operation_id; a genuinely "
                "different operation needs a new id)"
            )
            try:
                _write_operation(
                    resolved, operation_id, status="refused", todo_id=todo_id,
                    action=action, args_digest=args_digest, error=message,
                )
            except state_store.STATE_WRITE_FAILURES as je:
                # Nothing applied — a lost refusal note cannot duplicate.
                _log(
                    paths.logs_dir(resolved),
                    f"operation {operation_id}: refusal journal write "
                    f"failed: {je}",
                )
            raise AwfApiError(message) from e
        # F-HIGH: the effect landed. If the finish record cannot be
        # written, the outcome is not durably known — surface
        # recovery_required (a retry would read the open ``running``
        # record) instead of returning a result the journal cannot prove.
        try:
            _write_operation(
                resolved, operation_id, status="applied", todo_id=todo_id,
                action=action, args_digest=args_digest, result=result.as_dict(),
            )
        except state_store.STATE_WRITE_FAILURES as e:
            raise OperationRecoveryRequired(
                operation_id,
                f"operation {operation_id}: applied but the finish record "
                f"could not be written ({e}) — recovery_required, do not "
                "retry blindly",
            ) from e
        return result


def closure_files(project_dir: Path, todo_id: str) -> list[Path]:
    """All BLOCKED/ACK closure signal files for ``todo_id`` (canonical + legacy).

    Mirrors ``todos.is_closed`` file set (plus the ``.md`` companions and
    the BD-21 ``.md.ready`` typo form the engine also accepts). DONE-* is
    intentionally excluded: a DONE closure is lifted only by ``awf restore``.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    short = short_id(todo_id)
    candidates = [
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"BLOCKED-{todo_id}.md",
        outbox / f"BLOCKED-{todo_id}.md.ready",
        inbox / f"ACK-{todo_id}.ready",
    ]
    if short != todo_id:
        candidates += [
            outbox / f"BLOCKED-{short}.ready",
            outbox / f"BLOCKED-{short}.md",
            outbox / f"BLOCKED-{short}.md.ready",
            inbox / f"ACK-{short}.ready",
        ]
    return [p for p in candidates if p.is_file()]


def has_done_closure(project_dir: Path, todo_id: str) -> bool:
    """True if a DONE-* closure signal (canonical or legacy) is in the outbox."""
    short = short_id(todo_id)
    outbox = paths.outbox(project_dir)
    for tid in dict.fromkeys((todo_id, short)):
        if (outbox / f"DONE-{tid}.ready").is_file():
            return True
    return False


def clear_stale_closures(project_dir: Path, todo_id: str) -> tuple[list[str], Path | None]:
    """Move all BLOCKED/ACK closures of ``todo_id`` into a trace dir.

    Destination: ``context/unblock-<todo_id>-<timestamp>/`` — each call gets
    its own directory, so repeated unblocks never overwrite an earlier trace.
    Returns ``(moved basenames, trace dir)`` — both empty/None when there is
    nothing to move. DONE-* signals are never moved.
    """
    # REVIEW F4/G1: the trace LEAF is a destination too — check it before
    # mkdir. This mover touches inbox (ACK closures), outbox (BLOCKED
    # closures) and context (the trace dir) — ``done`` is NOT one of them.
    trace = paths.context_dir(project_dir) / f"unblock-{todo_id}-{_timestamp()}"
    refuse_unsafe_surfaces(
        project_dir,
        f"clear closures for {todo_id}",
        paths.inbox(project_dir),
        paths.outbox(project_dir),
        paths.context_dir(project_dir),
        trace,
    )
    files = closure_files(project_dir, todo_id)
    if not files:
        return [], None
    # REVIEW 0281 residual: the trace leaf was preflighted above; creating
    # it through the no-follow barrier refuses a link swapped in BEFORE this
    # mkdir. The ``shutil.move`` calls below stay path-based, so a directory
    # swapped in AFTER it is the movers' known residual (same class in
    # ``remove_todo``/``retire_todo``), not something this mkdir can close —
    # closing it needs dirfd moves, which this unit does not add.
    managed_mkdir_api(trace, operation=f"clear closures for {todo_id}")
    moved: list[str] = []
    for f in files:
        shutil.move(str(f), str(trace / f.name))
        moved.append(f.name)
    return moved, trace


def unblock_todo(project_dir: Path, todo_id: str) -> UnblockResult:
    """Clear stale BLOCKED/ACK closures so the TODO is active again.

    RUN3 #4: the manual repair for the TODO-0018 case — a stale
    ``BLOCKED-<id>.ready`` outlived a re-issue and hid the fresh TODO from
    ``awf_status``. Refused while the pipeline is running (moving a signal
    out from under a live engine would break its wait).
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # F-MED-2: the signal read-and-move is a read-modify-write — hold the
    # project lock so it cannot interleave with a concurrent writer.
    from .._lock import locked

    with locked(project_dir):
        # AUD-2 (TODO-0216 iter-4): the "pipeline running" guard is checked
        # UNDER the lock, like retire_todo and the durable update path —
        # checked-before-lock left a window for awf start to slip in and
        # then this op moves a live unit's signals out from under the engine.
        running, _pid, _tail = check_pipeline_running(project_dir)
        if running:
            raise AwfApiError(
                "pipeline is running — wait for verify/salvage, or awf kill first"
            )
        moved, trace = clear_stale_closures(project_dir, todo_id)
        if not moved:
            raise AwfApiError(
                f"No closure signals (BLOCKED/ACK) for {todo_id} — nothing to unblock. "
                "DONE closures are never lifted by unblock; use awf restore for an "
                "archived TODO."
            )

        trace_rel = trace.relative_to(project_dir).as_posix() if trace else ""
        _log(
            paths.logs_dir(project_dir),
            f"unblock: {todo_id} — cleared {', '.join(moved)} → {trace_rel}",
        )
    return UnblockResult(
        todo_id=todo_id,
        moved=moved,
        trace=trace_rel,
        message=(
            f"{todo_id} unblocked: {len(moved)} stale closure signal(s) "
            f"moved to {trace_rel}."
        ),
    )


def remove_todo(project_dir: Path, todo_id: str) -> RemoveTodoResult:
    """Delete a TODO that never started; the files keep a trace in done/.

    RUN3 #5: an inert TODO confuses ``awf status`` and blocks nothing —
    but it must not be erased silently either, so the file moves to
    ``done/<id>/removed-<timestamp>.md``.

    REPORTS26 B-f2 (TODO-0161): the dispatch ``.ready`` is an ARTIFACT of
    dispatch, not a sign of having started — it is no longer a refusal.
    The unit is removable when there is NO PROGRESS, NO outbound signal
    (DONE/BLOCKED/REVIEW and any ``*-<id>.*`` in the outbox) and NO
    inbound ACK/APPROVE. While removable, the ``.ready`` moves to the
    trace directory beside the renamed ``.md``, and
    ``context/BASELINE-<id>.*`` is DELETED (the unit no longer exists);
    the deleted files are reported in ``removed_files`` (project-relative
    paths). All other refusals are unchanged: a running pipeline, a
    missing ``.md``, a DONE closure (``awf restore``).
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # F-MED-2: the signal read-and-move is a read-modify-write — hold the
    # project lock across check + move so a concurrent writer cannot slip
    # between them.
    from .._lock import locked

    with locked(project_dir):
        # AUD-2 (TODO-0216 iter-4): the "pipeline running" guard is checked
        # UNDER the lock, like retire_todo and the durable update path —
        # checked-before-lock left a window for awf start to slip in and
        # then this op moves a live unit's files out from under the engine.
        running, _pid, _tail = check_pipeline_running(project_dir)
        if running:
            raise AwfApiError(
                "pipeline is running — wait for verify/salvage, or awf kill first"
            )
        # TODO-0240: a TODO the active run still plans to launch is a live
        # reference — removing the file would leave a dangling queue entry.
        # Replace or cancel it in the queue first, then remove. Both run
        # slots count (main + service), so no slot is left naming a gone
        # unit. Only the NOT-STARTED region counts (Z6): an element already
        # completed or current was launched, and a completed one is history —
        # the broad "still holding" predicate refused it with advice
        # ("replace it first") that replace itself cannot execute.
        holding_slot = run_state.queued_todo_slot(project_dir, todo_id)
        if holding_slot:
            raise AwfApiError(
                f"{todo_id} is in the active run queue ({holding_slot}) — "
                "replace it first (awf_run_revise with {'from': ..., 'to': "
                "...}) or finish the run (a service slot has no run_revise: "
                "close it with awf_run_service_finish), then remove the "
                "TODO."
            )
        inbox = paths.inbox(project_dir)
        outbox = paths.outbox(project_dir)
        context_dir = paths.context_dir(project_dir)
        md = inbox / f"{todo_id}.md"
        # REVIEW G2: a missing id is "not found", not "unsafe managed path" —
        # the existence check comes BEFORE the barrier so the diagnosis is
        # about the id the caller asked for.
        if not md.is_file():
            raise AwfApiError(f"{todo_id}.md not found in inbox — nothing to remove.")
        # REVIEW F4/G1: every surface this mover touches, LEAVES included:
        # inbox (md/.ready), outbox (signals), context (BASELINE deletion),
        # done/<id> (the archive leaf).
        done_dir = paths.done_dir(project_dir) / todo_id
        # REVIEW виток 3: the LEAF is checked too, not just ``done/<id>`` —
        # a symlinked directory at ``done/<id>/removed-<ts>.md`` would send
        # the move outside the project. Same shape as ``retire_todo`` (G3):
        # one timestamp, one path — the barrier checks the file moved below.
        trace = done_dir / f"removed-{_timestamp()}.md"
        refuse_unsafe_surfaces(
            project_dir,
            f"remove {todo_id}",
            inbox,
            outbox,
            context_dir,
            done_dir,
            trace,
        )

        short = short_id(todo_id)
        ids = (todo_id, short) if short != todo_id else (todo_id,)
        signals = _started_signal_names(project_dir, todo_id)
        if signals:
            names = ", ".join(signals)
            if any(name.startswith("DONE-") for name in signals):
                hint = "DONE — the TODO is closed as finished; use awf restore."
            else:
                hint = "stale BLOCKED/ACK — use awf unblock; orphans — awf reset --orphans."
            raise AwfApiError(
                f"{todo_id} has signals: {names} — it was started. {hint}"
            )
        if todos.has_progress(outbox, todo_id):
            raise AwfApiError(
                f"{todo_id} has PROGRESS — it was started. Use awf todo-retire."
            )

        managed_mkdir_api(done_dir, operation=f"remove {todo_id}")
        shutil.move(str(md), str(trace))
        trace_rel = trace.relative_to(project_dir).as_posix()

        # REPORTS26 B-f2: the dispatch .ready is an artifact — it follows the
        # .md into the trace directory instead of blocking the removal.
        ready = inbox / f"{todo_id}.ready"
        ready_moved = False
        if ready.is_file():
            # unique name: a re-dispatched number keeps its earlier trace copy
            ready_target = done_dir / _unique_name(done_dir, ready.name)
            shutil.move(str(ready), str(ready_target))
            ready_moved = True

        # The unit no longer exists — its baseline snapshot is dead weight
        # (commit gate / prove-red would read it for a TODO that is gone).
        removed_files: list[str] = []
        for tid in ids:
            for p in sorted(context_dir.glob(f"BASELINE-{tid}.*")):
                if p.is_file():
                    removed_files.append(p.relative_to(project_dir).as_posix())
        removed_files.sort()
        for rel in removed_files:
            (project_dir / rel).unlink()

        note = f"todo-remove: {todo_id} — {md.name} → {trace_rel}"
        if ready_moved:
            note += f", {ready.name} → {done_dir.name}/"
        if removed_files:
            note += f", deleted {len(removed_files)} baseline file(s)"
        _log(paths.logs_dir(project_dir), note)

    message = f"{todo_id} removed (never started). Trace: {trace_rel}."
    if ready_moved:
        message += f" The dispatch .ready moved to done/{todo_id}/."
    if removed_files:
        message += f" Deleted {len(removed_files)} baseline file(s)."
    return RemoveTodoResult(
        todo_id=todo_id,
        trace_path=trace_rel,
        removed_files=removed_files,
        message=message,
    )


def _unique_name(dest_dir: Path, name: str) -> str:
    """A name that does not exist in ``dest_dir`` yet (never overwrite).

    ``PROGRESS-TODO-0001.md`` → ``PROGRESS-TODO-0001.md``, then
    ``PROGRESS-TODO-0001-1.md``, ``PROGRESS-TODO-0001-2.md``, ...
    (shared mechanism: awf/_names.py)
    """
    return unique_name(dest_dir, name)


def retire_todo(project_dir: Path, todo_id: str, reason: str) -> RetireTodoResult:
    """Archive a rejected/abandoned ACTIVE TODO — no fake DONE signal.

    RUN5 #2 (battle case TODO-0035): the reject path writes
    ``DONE-<id>.{md,json}`` / ``PROGRESS-<id>.md`` / ``REVIEW-<id>.md`` to
    the outbox but NOT ``DONE-<id>.ready`` — ``todos.is_closed`` stays
    false, so ``awf status`` / ``awf brief`` keep listing the TODO as
    active forever. Neither ``unblock`` (only BLOCKED/ACK), ``todo-remove``
    (refuses with ``.ready``) nor ``reset --orphans`` (it has PROGRESS)
    covers this state.

    Moves the TODO's files to ``done/<id>/`` — the same archive
    ``awf restore`` reads, so the recovery path survives:

    - ``inbox/TODO-<id>.md``      → ``done/<id>/TODO.md`` (restore shape)
    - ``inbox/TODO-<id>.ready``   → ``done/<id>/`` (kept, not deleted)
    - ``outbox/PROGRESS-<id>.*``  → ``done/<id>/``
    - ``outbox/DONE-<id>.{md,json}`` (NOT ``.ready``) → ``done/<id>/``
    - ``outbox/REVIEW-<id>.*``    → ``done/<id>/``

    (canonical and legacy short id forms). Writes
    ``done/<id>/RETIRED-<timestamp>.md`` with the reason, who (supervisor),
    time and the moved-file list, and logs to the orchestrator log.

    Refusals (clear errors, nothing moved):
    - no ``TODO-<id>.md`` in inbox and no ``done/<id>/TODO.md`` — not found;
    - ``done/<id>/TODO.md`` exists — already archived (use ``awf restore``);
    - a live pipeline on this id (PID alive + ``state/current.yaml``
      ``todo_id``) — ``awf kill`` or wait first;
    - an active run slot (main or service) still PLANS to launch the TODO
      (not-started queue region) — replace it there first, or finish the run;
    - empty ``reason`` — required (it becomes the RETIRED note body).
    """
    _validate_id(todo_id)
    reason = (reason or "").strip()
    if not reason:
        raise AwfApiError(
            "reason is required — it is written to the RETIRED note "
            "(why the TODO is retired)"
        )
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # F-MED-2: the archive check + move is a read-modify-write — hold the
    # project lock across check + move.
    from .._lock import locked

    with locked(project_dir):
        inbox = paths.inbox(project_dir)
        outbox = paths.outbox(project_dir)
        md = inbox / f"{todo_id}.md"
        done_dir = paths.done_dir(project_dir) / todo_id
        archived_md = done_dir / "TODO.md"

        if archived_md.is_file():
            extra = (
                f" and a second copy sits in inbox/{md.name} — inspect manually"
                if md.is_file()
                else ""
            )
            raise AwfApiError(
                f"{todo_id} is already archived (done/{todo_id}/TODO.md{extra}) — "
                "use awf restore to bring an archived TODO back."
            )
        # REVIEW G2: a missing id is "not found", not "unsafe managed path".
        if not md.is_file():
            raise AwfApiError(
                f"{todo_id}.md not found in inbox and no done/{todo_id}/TODO.md — "
                "nothing to retire."
            )

        # REVIEW F4/G1/G3: the surfaces this mover touches, LEAVES included:
        # inbox (md/.ready), outbox (PROGRESS/DONE/REVIEW), done/<id> and the
        # RETIRED note file itself. ``context`` is not touched.
        ts = _timestamp()
        note = done_dir / f"RETIRED-{ts}.md"
        refuse_unsafe_surfaces(
            project_dir,
            f"retire {todo_id}",
            inbox,
            outbox,
            done_dir,
            note,
        )
        # Live pipeline on THIS id: the engine owns the files right now.
        live, pid = _live_pipeline_on(project_dir, todo_id)
        if live:
            raise AwfApiError(
                f"a live pipeline is running on {todo_id} (pid {pid}) — "
                "wait for it to finish or awf kill first"
            )
        # TODO-0240 REVIEW Y2: the same dangling-reference guard as
        # ``remove_todo`` — an active run slot (main or service) that still
        # PLANS to launch this unit would keep naming a moved file. A
        # launched/current unit is excluded: retire is the reject-recovery
        # path (``test_launch_unblocks_after_retire``).
        holding_slot = run_state.queued_todo_slot(project_dir, todo_id)
        if holding_slot:
            raise AwfApiError(
                f"{todo_id} is in the active run queue ({holding_slot}) — "
                "replace it first (awf_run_revise with {'from': ..., 'to': "
                "...}) or finish the run (a service slot has no run_revise: "
                "close it with awf_run_service_finish), then retire the "
                "TODO."
            )

        short = short_id(todo_id)
        ids = (todo_id, short) if short != todo_id else (todo_id,)

        # (source, archive name) pairs — inbox first, then outbox artifacts.
        candidates: list[tuple[Path, str]] = [(md, "TODO.md")]
        ready = inbox / f"{todo_id}.ready"
        if ready.is_file():
            candidates.append((ready, ready.name))
        for tid in ids:
            candidates.extend(
                (p, p.name) for p in sorted(outbox.glob(f"PROGRESS-{tid}.*")) if p.is_file()
            )
            for ext in (".md", ".json"):
                p = outbox / f"DONE-{tid}{ext}"
                if p.is_file():
                    candidates.append((p, p.name))
            candidates.extend(
                (p, p.name) for p in sorted(outbox.glob(f"REVIEW-{tid}.*")) if p.is_file()
            )

        managed_mkdir_api(done_dir, operation=f"retire {todo_id}")
        moved: list[str] = []
        pairs: list[tuple[str, str]] = []  # (from, to) — project-relative
        for src, name in candidates:
            target = done_dir / _unique_name(done_dir, name)
            shutil.move(str(src), str(target))
            to_rel = target.relative_to(project_dir).as_posix()
            moved.append(to_rel)
            pairs.append((src.relative_to(project_dir).as_posix(), to_rel))

        # The note path checked by the barrier above IS the file written
        # below — one timestamp, one name (architect-review: a second
        # ``_timestamp()`` here checked a path nothing ever wrote).
        note_lines = [
            f"# RETIRED: {todo_id}",
            "",
            f"- when: {ts}",
            "- by: supervisor (awf todo-retire)",
            f"- reason: {reason}",
            "",
            "## Moved to archive",
            "",
        ]
        note_lines += [f"- {src} → {to}" for src, to in pairs]
        note_lines.append("")
        # REVIEW G3: the last write must not follow a symlink either — the
        # note leaf is no-follow written (the barrier above already refused
        # a symlinked leaf; this closes the swap window and any caller that
        # skips the barrier).
        managed_fs.managed_write_text(
            note, "\n".join(note_lines), root=project_dir, mode=MODE_OWNER_EDITABLE
        )
        note_rel = note.relative_to(project_dir).as_posix()

        _log(
            paths.logs_dir(project_dir),
            f"retire: {todo_id} — {len(moved)} file(s) → "
            f"done/{todo_id}/ (note: RETIRED-{ts}.md, reason: {reason})",
        )
    return RetireTodoResult(
        todo_id=todo_id,
        moved=moved,
        retired_note=note_rel,
        message=(
            f"{todo_id} retired: {len(moved)} file(s) → done/{todo_id}/ "
            f"(note: RETIRED-{ts}.md)."
        ),
    )


def transfer_exclusions(
    project_dir: Path, todo_id: str
) -> tuple[set[str], set[str], set[str]]:
    """The transfer exclusions a unit adopted, resolved from its audit links:
    ``(carry_over, include, claim)`` paths.

    A baseline taken at dispatch excludes the adopted paths from
    ``BASELINE-<todo>.untracked``; a LATER re-baseline (``awf run_next``) or a
    baseline recompute must re-apply the SAME exclusions, or an adopted path
    is re-listed as pre-existing and the commit gate silently drops it from
    the unit commit. The three channels keep their own links
    (``BASELINE-<todo>.carry_over`` / ``.include`` / ``.claim``) so the
    resolution stays traceable; a missing/unreadable link resolves to the
    empty set (best-effort, like the dispatch-time write).
    """
    from ..claim_changes import claim_exclusion_paths, read_claim_link
    from ..include_untracked import read_include_list
    from ..reject_files import read_carry_over_link, read_reject_files

    carry_over: set[str] = set()
    origin = read_carry_over_link(project_dir, todo_id)
    if origin:
        carry_over = set(read_reject_files(project_dir, origin) or [])
    include = set(read_include_list(project_dir, todo_id) or [])
    claim: set[str] = set()
    origin = read_claim_link(project_dir, todo_id)
    if origin:
        claim = set(claim_exclusion_paths(project_dir, origin))
    return carry_over, include, claim


def _recompute_include_baseline(
    project_dir: Path, todo_id: str, include_files: list[str]
) -> None:
    """REPORTS26 B-f2 (TODO-0161): recompute the unit's include metadata
    in place (not-started units only — the caller checked).

    ``BASELINE-<id>.untracked`` is rewritten from the CURRENT untracked
    state minus the included files — the same exclusion the dispatch
    applies (the unit never started, so everything untracked now is
    pre-existing). ``BASELINE-<id>.include`` is rewritten as the include
    trace, or CLEARED when the list is empty (the files return to the
    untracked snapshot). The rest of the baseline (``.sha``/``.status``/
    logs) is untouched — the sha still pins the dispatch moment.

    TODO-0221 M2: the OTHER transfer channels keep their exclusions too —
    a carry-over / claim the unit adopted must not return to the untracked
    snapshot just because the include list was recomputed (the same
    invariant the dispatch baseline holds; resolved from the audit links).
    """
    from ..include_untracked import clear_include_audit, write_include_audit

    carry_over, _include, claim = transfer_exclusions(project_dir, todo_id)
    excluded = carry_over | claim | set(include_files)
    # The snapshot's writers must share ONE body — the same printed-form
    # bridge the dispatch baseline uses, or a non-ASCII adopted path survives
    # here (see pipeline._untracked_snapshot).
    from .pipeline import _untracked_snapshot

    untracked = _untracked_snapshot(project_dir, excluded)
    managed_write_api_text(
        paths.context_dir(project_dir) / f"BASELINE-{todo_id}.untracked",
        untracked,
        operation="rebuild baseline untracked",
        errors="surrogateescape",
    )
    if include_files:
        write_include_audit(project_dir, todo_id, include_files)
    else:
        clear_include_audit(project_dir, todo_id)


def backup_and_write_todo(
    project_dir: Path,
    md: Path,
    old_content: str,
    new_content: str,
    *,
    _lock_held: bool = False,
) -> str:
    """Back up the TODO, then write it guarded — ONE trace for every writer.

    :func:`update_todo`, :func:`review_append` (TODO-0240) and the DoD
    ``checklist apply`` (TODO-0244) write the same file: the order lives
    here once — the previous content lands in
    ``context/TODO-<id>.md.bak-<ts>`` BEFORE the new text, and a failed
    write removes the orphan backup (T1(b): catching only ``AwfApiError``
    left it behind on an ``OSError`` like ``FileExistsError``).

    The backup goes through the managed writer, not a plain ``write_text``
    (TODO-0234 REVIEW architect): it is the same family as the edit — a
    symlinked ``context`` used to send the old content out of the project,
    and the managed writer creates the directory through the verified
    parent. Returns the project-relative backup path; re-raises after the
    backup is gone.
    """
    context_dir = paths.context_dir(project_dir)
    bak_name = _unique_name(context_dir, f"{md.stem}.md.bak-{_timestamp()}")
    backup = context_dir / bak_name
    managed_write_api_text(
        backup,
        old_content,
        operation="backup todo",
        mode=MODE_OWNER_EDITABLE,
        errors="surrogateescape",
    )
    try:
        _write_todo_checked(
            project_dir, md, old_content, new_content, _lock_held=_lock_held
        )
    except Exception:
        try:
            backup.unlink()
        except OSError:
            pass
        raise
    return backup.relative_to(project_dir).as_posix()


def _next_review_round(text: str) -> int:
    """The next ``виток`` number — the appender's fallback matches the reader.

    Numbered history: ``max(existing) + 1``. Not ``len(sections) + 1``: a
    hand-written section may carry a suffix (``… виток 2 — заметка``) or the
    numbering may have a gap, and either way a length-based count repeats a
    number that is already in the file.

    Unnumbered history (AA1, TODO-0231 виток 7): a heading that declares no
    number is the round it sits in — the RUN-REPORT fallback reports ``iters:
    len(sections)`` for such a file, so the appender must advance the SAME
    fallback: next = ``len(sections) + 1``. The old ``0 + 1`` restarted at 1
    and wrote a duplicate «виток 1» over the section that already was round 1.

    The heading vocabulary comes from :func:`awf.todos.review_round` — the
    ONE reader of "what round does this heading declare" — so a unit whose
    history mixes the legacy ``## REVIEW-фикс 2`` spelling advances past
    those numbers too instead of restarting at 1 (architect TODO-0231: the
    appender and the RUN-REPORT counter used to read different vocabularies).
    """
    highest, count = todos.review_summary(text)
    if highest is not None:
        return highest + 1
    return count + 1


def _live_pipeline_on(project_dir: Path, todo_id: str) -> tuple[bool, int]:
    """True when a LIVE pipeline owns this TODO's files (``pid`` alongside).

    ONE liveness fact for every writer (TODO-0240 review): the engine writes
    ``state.todo_id`` only after the first stage starts, so the run slot's
    ``current`` is the other half of the same signal — either one naming this
    unit means the engine owns the file. EVERY active run slot counts, not
    just the main one: ``run_service_start`` writes ``state/service-run.yaml``
    with ``current`` BEFORE the first stage writes ``state.todo_id``, the same
    launch window; a predicate reading only the main slot let a review section
    land on a service-owned file (QA-X1). :func:`retire_todo` asks here
    directly; :func:`review_append` and :func:`update_todo` ask through
    :func:`unit_readiness`, so no writer can disagree about which unit is
    live. A pipeline on another id does not touch this TODO's files —
    allowed.
    """
    running, pid, _tail = check_pipeline_running(project_dir)
    if not running:
        return False, pid
    state = read_state(project_dir)
    if isinstance(state, dict) and state.get("todo_id") == todo_id:
        return True, pid
    for _slot, run in run_state.active_runs(project_dir):
        if str(run.get("current") or "") == todo_id:
            return True, pid
    return False, pid


def _started_signal_names(project_dir: Path, todo_id: str) -> list[str]:
    """Start signals of a unit, as file names (``[]`` = none) — ONE rule.

    :func:`update_todo` and :func:`review_append` ask here through
    :func:`unit_readiness` (TODO-0240 review V7); :func:`remove_todo` reuses
    the enumeration for its hints:
    an outbox ``*-<id>.*`` signal or an inbox ``ACK-``/``APPROVE-``
    closure. ``PROGRESS`` is NOT folded in — a progress file is a separate
    fact with its own refusal (``todos.has_progress``), and merging it into
    the names made the callers re-split the list to tell them apart.
    """
    outbox = paths.outbox(project_dir)
    inbox = paths.inbox(project_dir)
    short = short_id(todo_id)
    ids = (todo_id, short) if short != todo_id else (todo_id,)
    found = [
        p
        for tid in ids
        for p in sorted(outbox.glob(f"*-{tid}.*"))
        if p.is_file() and not p.name.startswith("PROGRESS-")
    ]
    found += [
        p
        for tid in ids
        for prefix in ("ACK-", "APPROVE-")
        if (p := inbox / f"{prefix}{tid}.ready").is_file()
    ]
    return list(dict.fromkeys(p.name for p in found))


class Readiness(NamedTuple):
    """The two facts «is this unit in flight?», read once.

    ``update_todo`` (refuses a started unit), ``review_append`` (needs a
    started one) and the DoD ``checklist apply`` need the SAME pair — the
    live-pipeline window and the start signals/PROGRESS. Each caller keeps
    its own refusal text and its own polarity; the facts come from here, so
    a caller cannot re-derive one half and let the other through.
    """

    live: bool
    pid: int
    started: list[str]
    progress: bool


def unit_readiness(project_dir: Path, todo_id: str) -> Readiness:
    """Read the in-flight facts for one unit: liveness + start signals."""
    live, pid = _live_pipeline_on(project_dir, todo_id)
    return Readiness(
        live=live,
        pid=pid,
        started=_started_signal_names(project_dir, todo_id),
        progress=todos.has_progress(paths.outbox(project_dir), todo_id),
    )


def not_started_reason(project_dir: Path, todo_id: str) -> str:
    """Why a unit is NOT a not-started candidate (``""`` means it is).

    The one readiness sentence for the DoD ``checklist apply``; the facts
    come from :func:`unit_readiness`, the same pair ``update_todo`` and
    ``review_append`` refuse or require on (TODO-0244).
    """
    facts = unit_readiness(project_dir, todo_id)
    if facts.live:
        return (
            f"a live pipeline is running on {todo_id} (pid {facts.pid}) — "
            "the engine owns the file right now"
        )
    if facts.started or facts.progress:
        return f"{todo_id} has started ({', '.join(facts.started) or 'PROGRESS'})"
    return ""


def review_append(
    project_dir: Path,
    todo_id: str,
    text: str,
    reason: str = "",
    operation_id: str | None = None,
) -> ReviewAppendResult:
    """TODO-0240: append a supervisor REVIEW-fix section to a STARTED unit.

    The plain :func:`update_todo` refuses a unit that already started; a
    review round needs the opposite — the supervisor fixes the task text
    AFTER the worker's attempt. This writer appends exactly one section
    ``## REVIEW-фикс (супервизор, виток N)`` where N is one past the
    highest existing round (a hand-written suffix or a gap in numbering
    never produces a duplicate), keeps a backup in
    ``context/TODO-<id>.md.bak-<ts>`` and leaves the number, ``.ready`` and
    the baseline untouched.

    Refusals (clear errors, nothing written):
    - an empty ``text``;
    - no ``TODO-<id>.md`` in the inbox;
    - the unit is archived (``done/<id>/TODO.md``) — a closed unit comes
      back only via ``awf restore``;
    - the unit never started (no PROGRESS/outbox signal/ACK/APPROVE) — a
      queued TODO takes :func:`update_todo` (``awf todo-update``), not a
      review fix;
    - a LIVE pipeline on this id — the engine owns the file right now, so
      the caller stops it first (``awf kill``).

    With ``operation_id`` the call is durable, like :func:`update_todo`
    (TODO-0240 Z2): the outcome is journalled under ``state/operations/``,
    so a repeat after a transport cut returns the recorded result instead
    of appending a SECOND section and advancing the round.
    """
    if operation_id is None:
        return _review_append_impl(project_dir, todo_id, text, reason)
    return _durable_operation(
        project_dir,
        operation_id,
        todo_id,
        "review_append",
        result_keys=frozenset(
            {"todo_id", "section", "round", "backup", "message"}
        ),
        replay=lambda stored: ReviewAppendResult(
            todo_id=stored["todo_id"],
            section=stored["section"],
            round=stored["round"],
            backup=stored["backup"],
            message=stored["message"],
        ),
        apply=lambda resolved: _review_append_impl(
            resolved, todo_id, text, reason, _lock_held=True,
        ),
        args={
            "text": (text or "").strip(),
            "reason": (reason or "").strip(),
        },
    )


def _review_append_impl(
    project_dir: Path,
    todo_id: str,
    text: str,
    reason: str = "",
    *,
    _lock_held: bool = False,
) -> ReviewAppendResult:
    """The review append itself; the durable wrapper calls it under its lock."""
    _validate_id(todo_id)
    body = (text or "").strip()
    if not body:
        raise AwfApiError(
            "text is empty — write the review fix (the section body), or "
            "omit the call"
        )
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    if _lock_held:
        return _review_append_locked(project_dir, todo_id, body, reason)
    from .._lock import locked

    with locked(project_dir):
        return _review_append_locked(project_dir, todo_id, body, reason)


def _review_append_locked(
    project_dir: Path,
    todo_id: str,
    body: str,
    reason: str,
) -> ReviewAppendResult:
    """The append body — the caller holds the project lock."""
    inbox = paths.inbox(project_dir)
    md = inbox / f"{todo_id}.md"
    # W1 (REVIEW виток 3): the archive is checked FIRST. A real archive
    # MOVES the inbox file away, so the "not found" branch would answer
    # a closed unit with the wrong refusal ("nothing to append to").
    archived = paths.done_dir(project_dir) / todo_id / "TODO.md"
    if archived.is_file():
        raise AwfApiError(
            f"{todo_id} is closed (done/{todo_id}/TODO.md exists) — a "
            "closed unit takes no review fix; use awf restore first."
        )
    if not md.is_file():
        raise AwfApiError(
            f"{todo_id}.md not found in inbox — nothing to append a "
            "review section to."
        )
    facts = unit_readiness(project_dir, todo_id)
    if facts.live:
        raise AwfApiError(
            f"a live pipeline is running on {todo_id} (pid {facts.pid}) — "
            "the engine owns the file right now; awf kill first, "
            "then append the review fix."
        )
    if not facts.started and not facts.progress:
        raise AwfApiError(
            f"{todo_id} has not started (no PROGRESS, no signal) — a "
            "review fix belongs to a STARTED unit; a queued TODO takes "
            "awf todo-update (awf_todo_update)."
        )

    old_content = md.read_text(encoding="utf-8", errors="surrogateescape")
    round_no = _next_review_round(old_content)
    heading = f"## REVIEW-фикс (супервизор, виток {round_no})"
    new_content = _apply_append(old_content, heading + "\n\n" + body)

    backup_rel = backup_and_write_todo(
        project_dir, md, old_content, new_content, _lock_held=True
    )

    why = f", reason: {reason.strip()}" if reason.strip() else ""
    _log(
        paths.logs_dir(project_dir),
        f"review-append: {todo_id} — {heading}, backup: {backup_rel}{why}",
    )

    return ReviewAppendResult(
        todo_id=todo_id,
        section=heading,
        round=round_no,
        backup=backup_rel,
        message=(
            f"{todo_id}: appended '{heading}' (backup: {backup_rel})."
        ),
    )


def _refuse_unreadable_checklist(todo_text: str) -> None:
    """Refuse a TODO write that breaks the frozen DoD block (F14/F15).

    ONE guard for every writer in this module — the full reword, the reword
    that carries its own block, and ``section_updates``. A body that opens
    an unterminated fence swallows the block (``block_of`` → None) and the
    gate reports ``checklist: none``: a unit with a DoD block passes with
    zero checks. A body that carries a second heading makes the gate run
    the first while a human reads two. Callers run it only when the unit
    HAD a block — a legacy unit without one stays writable.
    """
    try:
        validate_readable_block(todo_text)
    except ChecklistError as e:
        raise AwfApiError(f"checklist: {e}") from None


def _apply_append(text: str, block: str) -> str:
    """Append a block to the end of the TODO text, separated by one blank
    line (TODO-0162). The result always ends with a single newline.

    A block carrying the frozen DoD heading is refused: dispatch already
    put that one block at the end of the TODO, and a second copy would be
    read by a human while the gate keeps executing the first (TODO-0244).
    """
    if block_of(block) is not None:
        raise AwfApiError(
            f"cannot append a second '## {CHECKLIST_NAME}' block — the DoD "
            "block is frozen at dispatch; change it with `awf checklist "
            "apply` (a not-started unit) or re-dispatch"
        )
    return append_block(text, block)


def _apply_section_update(text: str, heading: str, body: str) -> tuple[str, bool]:
    """Replace the body of the ``## <heading>`` section with ``body``
    (TODO-0162), up to the next level-2 ``## `` line or EOF. When the
    heading is absent the section is APPENDED at the end (replace-or-
    append) and the second return value is True.

    The output is a deterministic function of (text, heading, body), so
    a repeated identical update is idempotent: the file comes out
    byte-identical. When the heading occurs several times, the FIRST
    occurrence is the one replaced.
    """
    key = heading.strip()
    body_core = body.strip("\n")
    pat = re.compile(r"^##\s+" + re.escape(key) + r"\s*$")
    if not text.strip():
        section = f"## {key}" + ("\n" + body_core if body_core else "")
        return section + "\n", True
    ends_nl = text.endswith("\n")
    lines = text.split("\n")
    if ends_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    h = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if h is None:
        section = f"## {key}" + (f"\n{body_core}" if body_core else "")
        return append_block(text, section), True
    n = next(
        (i for i in range(h + 1, len(lines)) if re.match(r"^##\s", lines[i])),
        len(lines),
    )
    mid = [lines[h]] + (body_core.split("\n") if body_core else [])
    if n < len(lines):
        mid.append("")
    out = lines[:h] + mid + lines[n:]
    if ends_nl:
        out.append("")
    return "\n".join(out), False


def update_todo(
    project_dir: Path,
    todo_id: str,
    content: str = "",
    reason: str = "",
    include_untracked: list[str] | None = None,
    append: str | None = None,
    section_updates: dict[str, str] | None = None,
    operation_id: str | None = None,
) -> UpdateTodoResult:
    """R1.2 (TODO-0216): durable-operation entry point around the reword.

    Without ``operation_id`` the call is the plain reword below. With one,
    the operation is named and journalled under ``state/operations/`` so a
    retry after a cancel/hang returns the ACTUAL outcome instead of
    re-running blindly:

    - a recorded ``applied`` replay returns the stored result (no second
      backup, no second append);
    - a recorded ``refused`` replay does NOT stick: the same id
      re-runs the operation (the refusal was a snapshot of the
      preconditions, not a verdict);
    - a ``running`` record with no finish means the outcome is unknown —
      :class:`OperationRecoveryRequired` is raised (→ recovery_required),
      never a duplicate effect.

    A refusal (an expected AwfApiError, e.g. the CAS version conflict) is
    recorded as ``refused``; any other exception leaves the record at
    ``running`` on purpose — a crash mid-write is exactly the unknown
    outcome the retry must detect.

    The id names the operation by itself here: unlike
    :func:`review_append`, the journal stores no payload digest, so a
    retry with the same id replays the recorded outcome even when the
    ``content`` differs. A caller that may resend different content uses
    a new ``operation_id``.
    """
    if operation_id is None:
        return _update_todo_impl(
            project_dir, todo_id, content, reason, include_untracked,
            append, section_updates,
        )
    return _durable_operation(
        project_dir,
        operation_id,
        todo_id,
        "update_todo",
        result_keys=frozenset({"todo_id", "backup", "message"}),
        replay=lambda stored: UpdateTodoResult(
            todo_id=stored["todo_id"],
            backup=stored["backup"],
            message=stored["message"],
        ),
        apply=lambda resolved: _update_todo_impl(
            resolved, todo_id, content, reason, include_untracked,
            append, section_updates, _lock_held=True,
        ),
    )


def _update_todo_impl(
    project_dir: Path,
    todo_id: str,
    content: str = "",
    reason: str = "",
    include_untracked: list[str] | None = None,
    append: str | None = None,
    section_updates: dict[str, str] | None = None,
    *,
    _lock_held: bool = False,
) -> UpdateTodoResult:
    """Reword a not-started TODO, keeping the number (RUN6 #4).

    Replaces the content of ``inbox/TODO-<id>.md`` in place: the number,
    the dispatch ``.ready`` signal and the baseline snapshot stay
    untouched (the baseline pins a git sha, not the text). The previous
    content is backed up byte-identical to
    ``context/TODO-<id>.md.bak-<timestamp>`` (unique name, never
    overwritten) and the operation is logged to the orchestrator log
    (with ``reason``, when given).

    REPORTS26 B-f1 (TODO-0162): two partial-edit modes for a
    not-started TODO, each keeping the number/``.ready``/baseline and
    writing a backup like a full reword:
    - ``append`` — a block is added to the END of the md, separated by
      one blank line; the previous text is untouched;
    - ``section_updates`` — a mapping ``{heading: new body}``: the BODY
      of the ``## <heading>`` section (up to the next level-2 ``## ``
      line or EOF) is replaced; an ABSENT heading is added as a new
      section at the end (replace-or-append). A repeated identical call
      is idempotent — the file comes out byte-identical.

    Exactly ONE content mode per call: ``content`` OR ``append`` OR
    ``section_updates``; two or more at once is refused, as is an empty
    ``append`` or an empty ``section_updates`` map. ``include_untracked``
    (below) is orthogonal and stays combinable with any content mode.

    REPORTS26 B-f2 (TODO-0161): ``include_untracked`` — the unit's
    pre-existing-untracked inclusion for a NOT-STARTED unit. Same
    validation as the dispatch (each path must exist, be untracked, be
    not gitignored, stay inside the project; all-or-nothing — the FIRST
    invalid path refuses BEFORE any side effect). Accepted, it
    recomputes ``BASELINE-<id>.untracked`` (current untracked state
    minus the included files) and rewrites the
    ``BASELINE-<id>.include`` trace; an EMPTY list clears the inclusion
    (the files return to the untracked snapshot). An empty ``content``
    is allowed WHEN ``include_untracked`` is given (and no ``append`` /
    ``section_updates``) — the TODO text is not touched (no backup) and
    the answer carries ``backup=""``.

    Refusals (clear errors, nothing written):
    - no ``TODO-<id>.md`` in the inbox;
    - two or more content modes at once (``content``/``append``/
      ``section_updates``); an empty ``append``; an empty
      ``section_updates`` map; an empty heading key in
      ``section_updates``; a ``section_updates`` key naming the frozen DoD
      block (any heading case); an ``append`` block carrying a second DoD
      block; a write that breaks the frozen DoD block (an unterminated
      fence in a new body, a second heading, or a block added by
      ``section_updates`` to a unit that had none);
    - no content mode at all AND no ``include_untracked``;
    - an invalid ``include_untracked`` path (before any side effect);
    - the TODO already started — any outbox signal (``PROGRESS-*``,
      ``BLOCKED-*``, ``DONE-*``, ``REVIEW-*``, canonical or legacy), an
      inbox ``ACK-``/``APPROVE-`` closure, or a non-empty ``PROGRESS`` —
      the unit is in flight: fix it via REVIEW/replan, or retire it and
      re-dispatch;
    - a live pipeline on this id (the engine owns the file right now —
      a pipeline on another id does not touch this TODO's files).
    """
    _validate_id(todo_id)
    content = content or ""
    has_content = bool(content.strip())
    if append is not None and not append.strip():
        raise AwfApiError(
            "append is empty — nothing to append (omit append to keep the "
            "text as is)"
        )
    if section_updates is not None and not section_updates:
        raise AwfApiError(
            "section_updates is an empty map — nothing to update (omit "
            "section_updates to keep the text as is)"
        )
    if section_updates is not None and any(not k.strip() for k in section_updates):
        raise AwfApiError(
            "section_updates: an empty heading key — name the ## heading "
            "to replace or add"
        )
    if section_updates is not None:
        # The block READER matches a heading case-insensitively
        # (``awf._markdown.find_headings``), so the guard uses the same rule: an
        # exact-case compare would let ``"чек-лист завершения"`` through and
        # the section updater (case-sensitive) would append a SECOND heading
        # at the end instead of touching the first one.
        frozen = CHECKLIST_NAME.casefold()
        for heading in section_updates:
            if heading.strip().casefold() == frozen:
                raise AwfApiError(
                    "section_updates cannot rewrite "
                    f"'## {CHECKLIST_NAME}' — the DoD block is frozen at "
                    "dispatch; re-dispatch the unit to change it"
                )
    modes = [
        name
        for name, on in (
            ("content", has_content),
            ("append", append is not None),
            ("section_updates", section_updates is not None),
        )
        if on
    ]
    if len(modes) > 1:
        raise AwfApiError(
            f"exactly one of content/append/section_updates per call — "
            f"got {', '.join(modes)}"
        )
    if not modes and include_untracked is None:
        raise AwfApiError(
            "content is empty and no include_untracked — nothing to update with"
        )
    sections = section_updates if section_updates is not None else {}
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    md = paths.inbox(project_dir) / f"{todo_id}.md"
    if not md.is_file():
        raise AwfApiError(f"{todo_id}.md not found in inbox — nothing to update.")

    # Live pipeline on THIS id: the engine owns the files right now.
    facts = unit_readiness(project_dir, todo_id)
    if facts.live:
        raise AwfApiError(
            f"a live pipeline is running on {todo_id} (pid {facts.pid}) — "
            "wait for it to finish or awf kill first"
        )

    if facts.started or facts.progress:
        names = ", ".join(facts.started) or "PROGRESS"
        raise AwfApiError(
            f"{todo_id} has signals: {names} — it is in flight. "
            "Fix the unit via REVIEW/replan, or retire it (awf todo-retire) "
            "and re-dispatch."
        )

    # REPORTS26 B-f2: the inclusion is validated BEFORE any side effect —
    # the dispatch's all-or-nothing contract (no backup, no snapshot
    # rewrite on refusal). The baseline is recomputed before the content
    # write, so a failed write still leaves a consistent include state.
    include_files: list[str] | None = None
    if include_untracked is not None:
        from ..include_untracked import resolve_include_untracked

        include_files = (
            [] if not include_untracked
            else resolve_include_untracked(project_dir, include_untracked)
        )
        _recompute_include_baseline(project_dir, todo_id, include_files)

    has_any_content_mode = has_content or append is not None or section_updates is not None
    if not has_any_content_mode:
        if include_files:
            what = (
                f"include_untracked: {len(include_files)} pre-existing file(s) "
                "re-claimed into the unit commit"
            )
        else:
            what = (
                "include_untracked cleared (files returned to the untracked "
                "snapshot)"
            )
        _log(
            paths.logs_dir(project_dir),
            f"todo-update: {todo_id} — {what}; baseline recomputed, content kept",
        )
        return UpdateTodoResult(
            todo_id=todo_id,
            backup="",
            message=f"{todo_id} updated: {what}; number/.ready/baseline sha kept.",
        )

    old_content = md.read_text(encoding="utf-8", errors="surrogateescape")

    if has_content:
        new_content = content
        what = f"content replaced ({len(content)} chars)"
        old_block = block_of(old_content)
        # S4/Q3: a full reword must not silently drop the frozen DoD block.
        # The new text's block is authoritative only when it actually CARRIES
        # items — a heading with no body is not a block (it would let the
        # unit pass with zero checks). Otherwise the old block is carried
        # over verbatim.
        if (
            old_block is not None
            and block_has_items(old_content)
            and not block_has_items(new_content)
        ):
            new_block = block_of(new_content)
            if new_block is not None:
                new_content = new_content.replace(new_block, "", 1)
            new_content = append_block(new_content, old_block)
            # F14 (TODO-0244): the carried block is what the gate will read —
            # it must be readable in the text actually written (a new body
            # ending in an unterminated fence would swallow it, leaving a
            # unit with a DoD block that reports `checklist: none`).
            _refuse_unreadable_checklist(new_content)
            what += ", frozen checklist block kept"
        elif block_of(new_content) is not None:
            # The new text brings its OWN block (the carry above did not run,
            # so that block is authoritative). A full reword is then a writer
            # of a block it did not compose — the same F14/F15 guard applies:
            # it must read back, and there must be exactly one. Without this
            # branch a reword could write the very two-block or unreadable
            # text the dispatch refuses.
            _refuse_unreadable_checklist(new_content)
    elif append is not None:
        new_content = _apply_append(old_content, append)
        what = f"appended ({len(append.strip())} chars)"
    else:
        new_content = old_content
        replaced: list[str] = []
        added: list[str] = []
        for heading, body in sections.items():
            new_content, is_added = _apply_section_update(new_content, heading, body)
            (added if is_added else replaced).append(heading.strip())
        # F14/F15 on the THIRD writer: a section body is free text, so it can
        # open an unterminated fence (the frozen block after it becomes
        # invisible) or carry its own ``## Чек-лист завершения`` heading. The
        # heading-key guard above only covers the direct route.
        if block_of(old_content) is not None:
            _refuse_unreadable_checklist(new_content)
        elif block_of(new_content) is not None:
            raise AwfApiError(
                f"section_updates cannot add a '## {CHECKLIST_NAME}' block — "
                "the DoD block is composed at dispatch; re-dispatch the unit"
            )
        parts = []
        if replaced:
            parts.append("replaced: " + ", ".join(replaced))
        if added:
            parts.append("added: " + ", ".join(added))
        what = "section_updates — " + "; ".join(parts)

    # T1(b): the backup must not outlive a failed write — the shared helper
    # owns the backup + guarded write + orphan cleanup order.
    backup_rel = backup_and_write_todo(
        project_dir, md, old_content, new_content, _lock_held=_lock_held
    )

    why = f", reason: {reason.strip()}" if reason.strip() else ""
    include_note = ""
    if include_files is not None:
        include_note = (
            f", include_untracked: {len(include_files)} file(s)"
            if include_files
            else ", include_untracked: cleared"
        )
    _log(
        paths.logs_dir(project_dir),
        f"todo-update: {todo_id} — {what}, "
        f"backup: {backup_rel}{include_note}{why})",
    )
    message = (
        f"{todo_id} updated: {what}, number/.ready/baseline "
        f"kept (backup: {backup_rel})."
    )
    if include_files is not None:
        message += (
            f" include_untracked: {len(include_files)} file(s)."
            if include_files
            else " include_untracked: cleared."
        )
    return UpdateTodoResult(
        todo_id=todo_id,
        backup=backup_rel,
        message=message,
    )


def blocked_draft(project_dir: Path, todo_id: str) -> BlockedDraftResult:
    """Build a ready ``## REVIEW-фикс`` draft from a BLOCKED note.

    TODO-0229 (M5): every blocked round used to cost the supervisor a manual
    rewrite of the note into a REVIEW-fix section. This reads
    ``outbox/BLOCKED-<id>.md`` (falling back to the legacy short name
    ``outbox/BLOCKED-<short>.md`` only when the canonical file is absent),
    takes its ``## НАХОДКИ`` and ``## НАПРАВЛЕНИЯ-ФИКСА`` sections and
    returns a paste-ready text with the supervisor's decision slots left
    empty. It does NOT write the TODO — the supervisor stays the one who
    decides scope and carries.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    todo = str(todo_id or "").strip()
    _validate_id(todo)
    outbox = paths.outbox(project_dir)
    note = outbox / f"BLOCKED-{todo}.md"
    if not note.is_file():
        # R6 (K6): a note written before the canonical name existed is still
        # readable — the short form is a fallback, never an override of the
        # canonical file.
        legacy = outbox / f"BLOCKED-{short_id(todo)}.md"
        if not legacy.is_file():
            raise AwfApiError(
                f"no BLOCKED note for {todo} ({note} or {legacy}) — nothing "
                "to draft a REVIEW-fix from"
            )
        note = legacy
    try:
        text = note.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise AwfApiError(f"cannot read {note}: {e}") from e

    findings = section_body(text, "НАХОДКИ")
    fixes = section_body(text, "НАПРАВЛЕНИЯ-ФИКСА")
    if not findings and not fixes:
        raise AwfApiError(
            f"{note.name} does not follow the BLOCKED schema — expected "
            "'## НАХОДКИ' / '## НАПРАВЛЕНИЯ-ФИКСА' sections "
            "(docs/unit-contract.md)"
        )

    review_text = "\n".join(
        [
            "## REVIEW-фикс",
            "",
            "**Находки:**",
            "",
            findings or "_нет_",
            "",
            "**Направления фикса:**",
            "",
            fixes or "_нет_",
            "",
            "Скоуп: …",
            "",
            "Переносы: …",
        ]
    ) + "\n"
    return BlockedDraftResult(
        todo_id=todo,
        review_text=review_text,
        note_path=str(note),
        message=(
            f"REVIEW-fix draft for {todo} built from {note.name} — paste it "
            "into the TODO and fill the Скоуп / Переносы slots."
        ),
    )


__all__ = [
    "blocked_draft",
    "closure_files",
    "clear_stale_closures",
    "has_done_closure",
    "remove_todo",
    "retire_todo",
    "review_append",
    "unblock_todo",
    "update_todo",
]
