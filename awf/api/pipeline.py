"""Pipeline API: start, continue, create_baseline, rollback, approve.

Foreground runs invoke ``awf.orchestrator.run_pipeline`` directly via
:class:`awf.api._background.PipelineArgs`. Background runs spawn a
detached subprocess via :func:`awf.api._background.start_in_background`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

from .. import budget as budget_mod
from .. import config as cfg_mod
from .. import git_utils, paths, stage_timeout, todos
from .._proc import caller_ancestry, child_pids, kill_pid_tree, ppid_of, run_tree
from ..infra import managed_fs
from ..pipeline_state import read_state
from ..signals import find_signal_file, short_id
from ..todo_ids import is_valid_todo_id
from ..unit_history import forget_unit_history
from ..verify import parse_argv
from ..versions import apply_handshake
from . import _lease, _liveness
from ._background import CHILD_PROBE_SECONDS, PipelineArgs, start_in_background
from ._errors import AwfApiError
from ._helpers import (
    managed_mkdir_api,
    managed_write_api_text,
    refuse_unsafe_surfaces,
    require_agentic,
    require_awf_project,
)
from ._results import (
    ApproveResult,
    BaselineResult,
    RejectResult,
    RollbackResult,
    StartResult,
)

# RUN8 #2: kill_pipeline's record of what it touched — survives the state
# clear (a separate file) and feeds the next launch's orphan warning.
LAST_KILL_FILE = "last-kill.json"


def _read_pid_cmdline(pid: int) -> str | None:
    """Read /proc/<pid>/cmdline; None when the process is gone/unreadable.

    Extracted as a seam for tests (PID-reuse race, QA .14).
    AUD04-07: delegates to the shared liveness module.
    """
    return _liveness.read_cmdline(pid)


def _is_pipeline_running(project_dir: Path) -> int | None:
    """DF5-6 + AUD04-07: state-source liveness via the shared resolver.

    Reads ``pipeline_pid`` from the state file. Identity is strict: the
    PID is ours only if its argv is ``... -m awf start|continue ...``
    (guards against PID reuse; an unreadable /proc never passes).
    Returns the live PID, or None if no pipeline / process is dead /
    PID reused.
    """
    state = read_state(project_dir)
    if not state:
        return None
    try:
        pid_int = int(state.get("pipeline_pid"))
    except (ValueError, TypeError):
        return None
    if not pid_int:
        return None
    if _liveness.pid_is_ours(pid_int, _read_pid_cmdline(pid_int), strict=True):
        return pid_int
    return None


def _is_background_child() -> bool:
    """This process IS the background pipeline child, not a caller of it.

    ``start_in_background`` spawns the child as ``<python> -m awf start ...``
    with ``AWF_BACKGROUND_CHILD=1`` in its environment. The marker is BOTH,
    because neither alone is reliable: the env var leaks into the child's
    descendants (workers, their shells and their tests — they must still be
    refused normally), and bare argv could be a human in the terminal. Only
    the process carrying both is the launched pipeline itself; it must not
    be refused by its own launch (its own PID file / its spawner's lease).

    The own cmdline is read from /proc directly, NOT via the read_cmdline
    seam: the seam models OTHER processes' argv for liveness tests and a
    blanket patch must not be able to flip the caller's own identity.
    Without /proc the argv cannot be checked — degrade to the env marker.
    """
    if not os.environ.get("AWF_BACKGROUND_CHILD"):
        return False
    try:
        cmdline = Path("/proc/self/cmdline").read_bytes().decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return True
    return _liveness.cmdline_is_ours(cmdline)


def _verify_child_alive(pid: int, log_file: Path | None = None) -> bool:
    """DF5-10: Wait briefly, then check if a background child is still alive.

    Returns True if the process is running after a short delay.
    Extracted as a standalone function so tests can mock it.
    """
    import os
    import time as _time

    _time.sleep(CHILD_PROBE_SECONDS)
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _reconcile(project_dir: Path) -> None:
    """DF6-2: Reconcile project state before start/continue.

    Ensures consistent state by cleaning up:
    1. Stale pipeline PID in state file → clear state.
    2. Duplicate ACK+APPROVE signals → remove older one.
    3. Multiple active TODOs → WARN ONLY, never archive (NEG-2026-09-19 R2:
       a run queue legitimately pre-readies several TODOs; the old
       auto-archive moved them to done/ with their .ready and the engine
       then ran the wrong one).

    Note: does NOT auto-archive TODOs based on ACK/APPROVE presence —
    that's done by orchestrator verify stage (DF6-1). Tests pre-create
    APPROVE signals for BD-8 auto-approval; auto-archiving would break them.
    """
    import os

    from .. import paths
    from .._log import log as _log
    from ..pipeline_state import drop_fields, read_state

    inbox = paths.inbox(project_dir)
    logs_dir = project_dir / ".agentic" / "logs"
    cleaned: list[str] = []

    # 1. Clear stale PID — but PRESERVE other state fields (stage_name, etc.)
    # so continue_pipeline can still resume from the correct stage.
    state = read_state(project_dir)
    if state and state.get("pipeline_pid"):
        pid_str = state["pipeline_pid"]
        try:
            pid_int = int(pid_str)
            os.kill(pid_int, 0)
        except (ProcessLookupError, PermissionError, ValueError, TypeError, OSError):
            # Remove just the PID, keep stage info for continue_pipeline.
            # R1.5: the removal goes through the current.yaml OWNER (a merge
            # cannot delete a key) — the state-file bytes still leave only
            # via the Store, never a raw yaml dump from the api layer.
            drop_fields(project_dir, "pipeline_pid", logs_dir=logs_dir)
            cleaned.append(f"cleared stale PID {pid_str}")

    # 2. Deduplicate ACK+APPROVE (keep APPROVE, remove ACK)
    # TODO-0235 (M3d): the unlink below is destructive on the INBOX surface —
    # a symlinked ``inbox`` used to delete an outside ACK. Barrier first,
    # leaf included.
    if inbox.is_dir():
        refuse_unsafe_surfaces(project_dir, "reconcile stale ACK", inbox)
        for ack in inbox.glob("ACK-*.ready"):
            # The signal→unit mapping is the SHARED one (``_signal_todo_id``;
            # architect-review M8), and the APPROVE lookup accepts the
            # canonical AND legacy spelling (``find_signal_file``, the same
            # rule every consumer uses). An inline name slice missed a
            # canonical APPROVE when the ACK carried the legacy short name
            # (``ACK-0009`` + ``APPROVE-TODO-0009``) and left a duplicate.
            todo_id = _signal_todo_id(inbox, ack.stem)
            if not todo_id:
                # No TODO file to map the short name against — fall back to
                # the name the signal carries (the old behavior for a junk ack).
                todo_id = ack.name.replace("ACK-", "").replace(".ready", "")
            approve = find_signal_file(inbox, "APPROVE", todo_id, ".ready")
            if approve is not None:
                refuse_unsafe_surfaces(project_dir, "reconcile stale ACK", ack)
                ack.unlink()
                cleaned.append(f"dedup: removed {ack.name} (APPROVE exists)")

    # 3. Multiple active TODOs → WARN ONLY (NEG-2026-09-19 R2).
    # Destructive cleanup is never right here: a run queue pre-readies all of
    # its items, and archives belong to the verify/approve flow only.
    if inbox.is_dir():
        active = sorted(inbox.glob("TODO-*.ready"))
        if len(active) > 1:
            names = ", ".join(p.stem for p in active)
            cleaned.append(f"multiple active TODOs (left as-is): {names}")

    if cleaned and logs_dir.is_dir():
        _log(logs_dir, f"DF6-2 reconcile: {'; '.join(cleaned)}")


def _newest_blocked_todo(project_dir: Path) -> str:
    """Day-2 B3: newest TODO closed by BLOCKED (no DONE). ``""`` if none."""
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    if not inbox.is_dir():
        return ""
    candidates: list[tuple[int, str]] = []
    for ready in inbox.glob("TODO-*.ready"):
        todo_id = ready.stem
        # The «unit is live» halves are the SAME predicates the ACK/APPROVE
        # consumer uses — an inline copy drifts (and crashes on a stat race
        # the helpers already treat as «absent»). The blocked signal is this
        # reader's own extra condition (architect-review M8).
        if not _unit_file_present(inbox, todo_id):
            continue
        short = short_id(todo_id)
        blocked = any((outbox / f"BLOCKED-{cid}.ready").exists() for cid in (todo_id, short))
        if blocked and not _unit_cycle_done(outbox, todo_id):
            try:
                num = int(short)
            except ValueError:
                num = 0
            candidates.append((num, todo_id))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _todo_closed(project_dir: Path, todo_id: str) -> bool:
    """True when closure signals (DONE/BLOCKED/ACK) mark the TODO closed."""
    return todos.is_closed(paths.inbox(project_dir), paths.outbox(project_dir), todo_id)


def _signal_todo_id(inbox: Path, stem: str) -> str:
    """The TODO id an inbox signal file (``stem``, no ``.ready``) addresses.

    One home for the decision-signal → TODO mapping (architect-review M8):
    the full id passes through, a legacy short form (``ACK-0009.ready``)
    maps by the short id of the ready TODO. The consumer
    (:func:`_resolve_pending_closure`), the noop-hint reader
    (:func:`_newest_armed_ack` / :func:`_armed_ack_file`) and the REVIEW
    reader (:func:`_pending_review_todo`) all ask here, so they can never
    disagree about which unit a signal belongs to.
    """
    candidate = stem.split("-", 1)[1] if "-" in stem else ""
    if candidate.startswith("TODO-"):
        return candidate
    for ready in inbox.glob("TODO-*.ready"):
        if short_id(ready.stem) == candidate:
            return ready.stem
    return ""


def _unit_file_present(inbox: Path, target: str) -> bool:
    """The unit behind a decision signal has its TODO file in the inbox.

    The FIRST half of «a signal can be consumed» — one home for the three
    callers that need it separately for their own diagnostics (the ACK
    arming path answers «no TODO file», the consumer and the REVIEW reader
    just skip). An unreadable surface is «absent» — a refusal there must
    not crash the resume path.
    """
    md = inbox / f"{target}.md"
    try:
        return md.is_file() and md.stat().st_size > 0
    except OSError:
        return False


def _unit_cycle_done(outbox: Path, target: str) -> bool:
    """The unit behind a decision signal already finished its cycle.

    The SECOND half of «a signal can be consumed» (architect-review M8,
    A5): a ``DONE`` in the outbox (full or legacy short id) means nothing
    is waiting for an answer — the signal stays on disk for the archive
    flow. Shared by the ACK arming path's diagnostic and the consumer's
    filter, so the two can never disagree about what counts as completed.
    """
    short = short_id(target)
    return any(
        (outbox / f"DONE-{cid}.ready").exists() for cid in (target, short)
    )


def _signal_is_consumable(inbox: Path, outbox: Path, target: str) -> bool:
    """Whether a signal for ``target`` can still be consumed.

    One home for the filter both the consumer and the noop-hint apply
    (architect-review M8, A5): the unit needs a non-empty TODO file, and a
    completed cycle (``DONE`` in the outbox) is not waiting for an answer.
    Composed of the same two halves the ACK arming path reports separately.
    """
    return _unit_file_present(inbox, target) and not _unit_cycle_done(
        outbox, target
    )


def _armed_ack_file(inbox: Path, target: str) -> Path | None:
    """The ACK signal waiting for ``target`` in ``inbox``, or ``None``.

    ARCHITECT-REVIEW M8: found through the SAME mapping the consumer uses
    (:func:`_signal_todo_id`), not through the canonical file spelling — a
    legacy short-name ack (``ACK-0009.ready``) is consumable for TODO-0009
    too, so the noop hint must neither deny nor misname it. Without this
    the extracted mapping would have a bypass and the two readers could
    disagree.
    """
    try:
        for p in inbox.glob("ACK-*.ready"):
            if p.is_file() and _signal_todo_id(inbox, p.stem) == target:
                return p
    except OSError:
        return None
    return None


def _resume_target(project_dir: Path, pin: str = "", ack: str = "") -> str:
    """The unit a launch resumes: explicit pin > ack > state > newest active.

    ONE home for the priority (KAUD-8, RUN8 #1). Both the readiness gate and
    the launch itself resolve through it: a second copy is how the gate ends
    up checking a different unit than the one that starts.
    """
    if pin.strip():
        return pin.strip()
    if ack.strip():
        return ack.strip()
    state = read_state(project_dir) or {}
    return str(state.get("todo_id") or "") or (todos.newest_active(project_dir) or "")


def _resume_stage(from_stage: str | None, state: dict | None) -> str:
    """The stage a resume starts from: the explicit parameter wins, else the
    stage the state file recorded (DF5-2).

    ONE rule for both the launch and the review-pass budget gate: reading
    only the parameter let a state-recorded implementer resume slip past the
    cap (the common ``awf continue`` call passes no stage).
    """
    if from_stage:
        return from_stage
    return str((state or {}).get("stage_name") or "")


def _budget_refusal(project_dir: Path, todo_id: str, action: str) -> StartResult | None:
    """The review-pass budget refusal of a launch, or ``None`` to go on.

    ONE body for the start and continue gates (TODO-0262): a broken limit and
    an unwritable note are loud refusals; an exhausted unit is a state noop
    with the owner-facing advice. Only the refused action's name differs — a
    second copy of the three branches would drift.
    """
    try:
        reason = budget_mod.check_and_note(
            project_dir, todo_id, cfg_mod.load(project_dir)
        )
    except budget_mod.BudgetConfigError as e:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=1,
            message=f"limits.max_review_passes refused: {e}",
        )
    except OSError as e:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=1,
            message=f"Cannot write the budget note: {e}",
        )
    if not reason:
        return None
    return StartResult(
        run_mode="noop",
        run_id=None,
        log_file=None,
        exit_code=0,
        message=(
            f"{reason} — {action} refused; the unit goes to the owner "
            f"(see outbox/BUDGET-{todo_id}.md)."
        ),
    )


def _spec_gate_for_launch(
    project_dir: Path, todo_id: str, pipeline: str = ""
) -> str:
    """SPEC IMP3 (K3): the readiness refusal for a launch, or ``""``.

    The SAME predicate dispatch uses (``unit_contract.spec_package_gate``)
    reads the unit's contract from its inbox TODO. A unit without
    ``spec_package`` is untouched (legacy path) — AUD3: the key's presence
    decides, so a legacy contract with an unrelated parse error keeps the
    pre-unit behavior instead of a "spec gate" refusal. Once the unit
    declares the package, any read/parse failure IS a readiness refusal: a
    launch must never pass a unit whose package claims cannot be read.
    Called BEFORE the lease, the PID and the stage.

    S14: a PRODUCER unit (``spec_package`` plus the spec pipeline) is not
    consumer-gated — its run is what makes the package READY. The pipeline
    name is resolved in the engine's order: the unit's frozen snapshot
    (ORCH M3.3 — what a resume actually runs), then the explicit launch
    argument, then the TODO's own ``pipeline:`` key, then the project
    default. A resume after a crash or ``awf kill`` names no pipeline at
    all, so the snapshot is what keeps the producer a producer there.
    """
    if not todo_id:
        return ""
    path = paths.inbox(project_dir) / f"{todo_id}.md"
    if not path.is_file():
        return ""
    from ..pipeline import active_pipeline_name, snapshot_pipeline_name
    from ..unit_contract import (
        declares_spec_package,
        is_spec_producer,
        parse_todo_contract,
        spec_package_gate,
    )

    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return f"TODO {todo_id}: {e}"
    if not declares_spec_package(content):
        return ""
    # S14: the launch pipeline is known BEFORE the parse — a producer may
    # omit ``source_refs``, and any spec pipeline (snapshot, argument, the
    # TODO's own key or a spec default) makes the unit a producer.
    hint = snapshot_pipeline_name(project_dir, todo_id)
    if not hint:
        hint = str(pipeline or "").strip()
    if not hint:
        try:
            hint = active_pipeline_name(project_dir, cfg_mod.load(project_dir))
        except (OSError, ValueError):
            hint = ""
    try:
        contract, _unknown = parse_todo_contract(content, pipeline=hint)
    except ValueError as e:
        return f"TODO {todo_id}: {e}"
    if is_spec_producer(contract, hint):
        return ""
    return spec_package_gate(project_dir, contract)


def _spec_gate_refusal(gate_message: str) -> StartResult:
    """The shared no-op answer for a readiness refusal (K3, D2).

    Start and continue are the same refusal surface: one text, one shape.
    """
    return StartResult(
        run_mode="noop",
        run_id=None,
        log_file=None,
        exit_code=0,
        message=f"Launch refused by the spec gate: {gate_message}",
    )


def _invalid_todo_refusal(surface: str, todo_id: str) -> StartResult:
    """P3 (TODO-0257 REVIEW): refuse a malformed unit id BEFORE any stage.

    The id becomes a path component and a report/journal string as soon as
    the engine runs. An inbox file name is not validated by ``newest_active``
    (``TODO-*.ready`` glob), so a non-UTF-8 name would reach the fail-closed
    writers raw. The writers still survive a corrupted name (defence in
    depth); the launch itself is refused here, like dispatch/run do.
    """
    return StartResult(
        run_mode="noop",
        run_id=None,
        log_file=None,
        exit_code=1,
        message=(
            f"Invalid TODO id for {surface}: {todo_id!r} "
            "(expected TODO-NNNN)."
        ),
    )


def _arm_ack_file(inbox: Path, ack: str) -> Path:
    """Write the ACK for ``ack`` — or reuse the one already waiting.

    M2 (REVIEW виток 10): a legacy short-name signal (``ACK-0009.ready``)
    maps to the same unit through the consumer's own reader; writing the
    canonical spelling next to it would leave a duplicate the consumer
    never eats (a second ACK can answer a FUTURE blocked cycle). The
    returned path is the file the consumer will consume.
    """
    # AUD2 (REVIEW 0281): the ACK is the supervisor's answer to a blocked
    # unit — the same managed barrier as the approve marker: a symlinked
    # ``inbox`` or a linked ACK leaf must refuse, never write outside.
    #
    # QA-0281 (REVIEW-FIX): the barrier runs BEFORE the reuse lookup. A
    # ``Path.glob`` on a symlinked inbox FOLLOWS the link, so a foreign
    # ``ACK-<id>.ready`` sitting outside the project used to be RETURNED
    # as the supervisor's answer — no write, but the refusal never ran and
    # the engine would consume a file from outside the project.
    managed_mkdir_api(inbox, operation=f"ack {ack}")
    existing = _armed_ack_file(inbox, ack)
    if existing is not None:
        return existing
    path = inbox / f"ACK-{ack}.ready"
    managed_write_api_text(path, "", operation=f"ack {ack}")
    return path


def _newest_armed_ack(project_dir: Path) -> str:
    """M8 (UX-щель): the TODO id of the newest CONSUMABLE ``ACK-*.ready``
    waiting in the inbox (the supervisor's pending answer) — ``""`` when
    none.

    A5 (REVIEW виток 2): the filter is the consumer's OWN
    (:func:`_signal_todo_id` + :func:`_signal_is_consumable`) — only
    ACTIVE/CURRENT units count. Advertising an ack whose TODO file is gone
    or whose unit is already completed would tell the supervisor to kill a
    live pipeline for an answer that can never be consumed.
    """
    inbox = paths.inbox(project_dir)
    if not inbox.is_dir():
        return ""
    try:
        ready = [p for p in inbox.glob("ACK-*.ready") if p.is_file()]
        ready.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    outbox = paths.outbox(project_dir)
    for sig_path in ready:
        target = _signal_todo_id(inbox, sig_path.stem)
        if target and _signal_is_consumable(inbox, outbox, target):
            return target
    return ""


def _pending_review_todo(project_dir: Path) -> str:
    """Day-2 review follow-up: newest TODO with a supervisor REVIEW and no DONE.

    ``REVIEW-{todo}.md`` in the outbox means the supervisor rejected the work —
    if the process died before it was consumed, the answer would otherwise be a
    silent dead-end. Returns ``""`` when there is no pending review.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    if not inbox.is_dir():
        return ""
    candidates: list[tuple[int, str]] = []
    for review in outbox.glob("REVIEW-*.md"):
        # A REVIEW is a decision signal like ACK/APPROVE, so the mapping and
        # the «TODO file present» half come from the SAME helpers the
        # consumer and the noop hint use (``_signal_todo_id`` /
        # ``_unit_file_present``) — a third private copy would drift.
        todo_id = _signal_todo_id(inbox, review.stem)
        if not todo_id or not _unit_file_present(inbox, todo_id):
            continue
        # The cycle-done half comes from the SAME helper the ACK/APPROVE
        # consumer uses (``_unit_cycle_done``) — an inline ``DONE-<id>.ready``
        # check would miss the legacy short form (``DONE-0009.ready``) and
        # report a completed unit's review as pending (architect-review M8).
        if _unit_cycle_done(outbox, todo_id):
            continue  # cycle completed — the review is stale
        try:
            num = int(short_id(todo_id))
        except ValueError:
            num = 0
        candidates.append((num, todo_id))
    if not candidates:
        return ""
    candidates.sort(reverse=True)
    return candidates[0][1]


def _resolve_pending_closure(project_dir: Path, todo_id: str = "") -> tuple[str, str]:
    """Day-2 B3: resume a TODO that is closed by a pending ACK/APPROVE.

    An ACK/APPROVE in the inbox means the supervisor already answered, but
    the orchestrator may have died before consuming it (interactive waits
    live only as long as the process). ``todos.is_closed`` then hides the
    TODO from ``newest_active`` and ``awf continue`` dead-ends.

    Consumes the signal, clears a BLOCKED closure if present, and returns
    ``(todo_id, note)``. With ``todo_id`` set, only that TODO's signals are
    considered — an APPROVE saved for a live verify stage is never touched
    (that path finds the TODO active and skips this resolver entirely).

    Returns ``("", "")`` when there is nothing to resume.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    if not inbox.is_dir():
        return "", ""

    signals = sorted(inbox.glob("ACK-*.ready")) + sorted(inbox.glob("APPROVE-*.ready"))
    for sig_path in signals:
        # The signal→unit mapping and the consumability filter are SHARED
        # with the noop-hint reader (``_newest_armed_ack``) — architect-review
        # M8: one home, so the hint can never advertise an answer this loop
        # would skip.
        target = _signal_todo_id(inbox, sig_path.stem)
        if not target:
            continue
        if todo_id and target != todo_id:
            continue
        if not _signal_is_consumable(inbox, outbox, target):
            continue  # no TODO file, or a completed cycle — not an answer
        short = short_id(target)
        # TODO-0235 (M3d): consuming the decision is an unlink on INBOX —
        # a symlinked surface/leaf must refuse, not delete outside. REVIEW B2:
        # the same call then moves BLOCKED leaves from OUTBOX into CONTEXT
        # (``_unblock_todo``), so those surfaces and leaves belong to the
        # same barrier — otherwise a symlinked outbox loses foreign files.
        clears = (outbox / f"BLOCKED-{target}.ready").exists() or (
            outbox / f"BLOCKED-{short}.ready"
        ).exists()
        closure_leaves: list[Path] = []
        if clears:
            ctx = paths.context_dir(project_dir)
            for cid in (target, short):
                for ext in (".ready", ".md"):
                    closure_leaves.append(outbox / f"BLOCKED-{cid}{ext}")
                    closure_leaves.append(ctx / f"BLOCKED-{cid}{ext}")
        refuse_unsafe_surfaces(
            project_dir,
            "consume pending closure signal",
            inbox,
            sig_path,
            outbox,
            paths.context_dir(project_dir),
            *closure_leaves,
        )
        try:
            sig_path.unlink()
        except OSError:
            continue
        note = f"consumed {sig_path.name}"
        if any((outbox / f"BLOCKED-{cid}.ready").exists() for cid in (target, short)):
            from ..pipeline_engine import _unblock_todo

            if _unblock_todo(
                project_dir, target, paths.agentic_dir(project_dir) / "logs"
            ):
                note += ", BLOCKED cleared"
            else:
                note += ", BLOCKED not cleared (unsafe path or move failed)"
        return target, note
    return "", ""


# ─── approve_commit ─────────────────────────────────────────────────────


def _service_unit_refusal(todo_id: str) -> str:
    """ORCH M5.2 (review fix F1): the refusal text for routing a service-run
    unit's verdict through the generic approve/reject path.

    The generic path writes the MAIN run's diary (outcomes/decisions),
    counts rejects there and escalates a double-reject into stop_run of the
    MAIN run — all wrong for a unit whose queue lives in
    ``state/service-run.yaml`` (the main run must stay byte-identical
    through a service run). The refusal is a NOOP with text (doctrine 03:
    a refusal is not an exception and not a hidden routing) — no signal,
    no REVIEW file, no verdict, no counter.
    """
    return (
        f"{todo_id} is a service-run unit (its queue lives in "
        "state/service-run.yaml, not in the main run) — the generic "
        "approve/reject path writes the MAIN run's diary and must not be "
        "used for it. Service verdict path: awf_run_service_approve("
        "evidence=...) approves the unit at its verify stage (verdict "
        "recorded in the service slot only). To REJECT the candidate: do "
        "not approve — close the service run (awf_run_service_finish with "
        "the reason) and re-issue awf_run_service_start with a corrected "
        "description. No signal, no REVIEW file, no verdict was written; "
        "the main run was not touched."
    )


def _is_service_unit(project_dir: Path, todo_id: str) -> bool:
    """ORCH M5.2 (review fix F1): True when ``todo_id`` is owned by the
    service slot's queue (``run_state.run_slot_for_todo`` == "service").
    A corrupt or absent service file degrades to False — the main path
    proceeds (the pre-M5.2 behavior)."""
    from .. import run_state

    return run_state.run_slot_for_todo(project_dir, todo_id) == "service"


def approve_commit(
    project_dir: Path,
    todo_id: str,
    *,
    evidence: str = "",
    verified_sha: str = "",
    confirm_degraded: bool = False,
) -> ApproveResult:
    """Create APPROVE-{todo_id}.ready signal to authorize auto-commit.

    SPEC A-run.3/A-run.8: in run (забег) mode the approve MUST carry the
    supervisor's independent-verification evidence — commands actually run
    and the verdict. It is stored to
    ``.agentic/context/RUN-EVIDENCE-{todo_id}.md`` so the owner can audit
    the approver. Outside a run the parameter is optional.

    U11 (B5, verified-sha): optional ``verified_sha`` — the working-tree
    fingerprint the supervisor recorded at verify time
    (``git_utils.tree_fingerprint`` / ``awf tree-sha``). When passed, the
    CURRENT fingerprint is recomputed and a mismatch (new commit, edited
    tracked file, new/changed untracked file) refuses the approve: the
    commit gate would commit something that was not verified. On match the
    fingerprint is stored to ``.agentic/context/VERIFIED-{todo_id}.sha``.
    Without the parameter the behavior is exactly as before.

    V-03 (re-verification 2026-09-27, owner decision): inside an ACTIVE
    run the parameter is REQUIRED — ``awf_approve(evidence=...,
    verified_sha='')`` used to publish the signal anyway, and the commit
    gate committed a tree the supervisor never pinned with a fingerprint.
    In run mode an empty ``verified_sha`` is an explicit refusal (no
    APPROVE signal, no verdict). Outside a run it stays optional.

    Requires ``.agentic/`` (consistency with other api functions).
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(f"invalid todo_id '{todo_id}', expected format TODO-NNNN")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # ORCH M5.2 (review fix F1): a service-run unit's verdict goes through
    # the service path only — refused BEFORE any side effect (no evidence
    # file, no fingerprint, no APPROVE signal, no diary entry in the MAIN
    # slot).
    if _is_service_unit(project_dir, todo_id):
        return ApproveResult(
            todo_id=todo_id,
            signal_file="",
            message=_service_unit_refusal(todo_id),
        )

    from ..run_state import read_run

    run = read_run(project_dir)
    run_active = bool(run and run.get("active"))
    evidence_file = paths.context_dir(project_dir) / f"RUN-EVIDENCE-{todo_id}.md"
    verified_file = paths.context_dir(project_dir) / f"VERIFIED-{todo_id}.sha"
    inbox = paths.inbox(project_dir)
    signal = inbox / f"APPROVE-{todo_id}.ready"

    # R2.4 (roadmap R2): in an active run the approval is bound to a
    # MATERIALISED CANDIDATE, not to the dirty worktree. The candidate tree
    # is built through the R2.3 assembler (isolated index, the user's index
    # is never opened) and the mandatory candidate check runs in a throwaway
    # worktree — a green run in the original dirty worktree does NOT replace
    # it. The evidence (candidate tree OID, contract digest, environment
    # digest, generation, unit revision, degraded paths) is stored and its
    # digest rides in the APPROVE binding; the gate re-derives it and refuses
    # on any change.
    from .. import candidate_verify as _candidate_verify
    from .. import commit_plan as _commit_plan

    def _discard_candidate_evidence() -> None:
        # A-04 (audit M1 / PA-1): a refused approve must leave NO evidence
        # artefact. The evidence is written only on a successful verdict
        # (below), but a stale CANDIDATE-<todo>.json from an EARLIER
        # successful approve would survive a refusal — verify_pack reads it
        # as a valid candidate and misreports a refused unit as approved.
        # One home for the invariant: clean on ENTRY, before any precheck
        # (missing evidence= / missing verified_sha=) or verdict can refuse,
        # so no refusal branch can be forgotten (the M1 → PA-1 recurrence).
        # REVIEW B3: it is still an unlink under ``.agentic/context`` — the
        # surface and the leaf are checked BEFORE the delete, so a symlinked
        # context refuses with the foreign file intact.
        evidence = _candidate_verify.evidence_path(project_dir, todo_id)
        refuse_unsafe_surfaces(
            project_dir,
            "discard candidate evidence",
            paths.context_dir(project_dir),
            evidence,
        )
        try:
            evidence.unlink()
        except OSError:
            pass

    _discard_candidate_evidence()

    verified_fp = ""
    if verified_sha.strip():
        v = verified_sha.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", v):
            raise AwfApiError(
                f"invalid verified_sha '{verified_sha}' — expected the 64-hex "
                "tree fingerprint from `awf tree-sha` (record it at verify "
                "time, before your checks)."
            )
        try:
            current_fp = git_utils.tree_fingerprint(project_dir)
        except RuntimeError as e:
            raise AwfApiError(
                "verified_sha was passed but the tree fingerprint could not "
                f"be computed: {str(e).splitlines()[0] if str(e) else e}. Is "
                f"{project_dir} a git repo with at least one commit?"
            ) from e
        if current_fp != v:
            raise AwfApiError(
                f"verified_sha mismatch: the tree changed after verification "
                f"(verified {v[:12]}…, now {current_fp[:12]}…). The commit "
                "gate would commit something you did not verify. Re-verify "
                "on the current tree (`awf tree-sha`) and approve with the "
                "fresh fingerprint."
            )
        verified_fp = current_fp

    # K9 (carry-cd R9, arch-4 point 1): the read-only requirements run
    # BEFORE the first effect. A missing evidence/verified_sha is a call
    # error, not an unsafe path — the barrier below must not mask it.
    if run_active and not evidence.strip() and not evidence_file.is_file():
        raise AwfApiError(
            "Run mode requires independent-verification evidence. Pass the "
            "commands you actually ran and the verdict via evidence=..., e.g. "
            "evidence='pytest -q → 348 passed; ruff check → clean; verdict: approve'."
        )

    # V-03 (re-verification 2026-09-27, owner decision): in an active run
    # the approve must ALSO carry the verified-tree fingerprint. The check
    # runs after the evidence one, so a call missing both still gets the
    # evidence error (the earlier, weaker gap). No APPROVE signal, no
    # verdict — the refusal happens before any publication.
    if run_active and not verified_sha.strip():
        raise AwfApiError(
            "Run mode requires verified_sha: the working-tree fingerprint "
            "you recorded at verify time (`awf tree-sha` / `awf_tree_sha`, "
            "BEFORE your checks). Approve without it is refused — the "
            "commit gate would commit a tree you did not verify."
        )

    # K9 (carry-cd R9): the preflight of the surfaces the approval writes
    # runs BEFORE RUN-EVIDENCE / VERIFIED and before managed_mkdir — a
    # symlinked ``inbox`` or a symlinked APPROVE leaf refuses with no new
    # file (INV1). The fingerprint must never be rolled back, so the
    # refusal cannot come after it.
    refuse_unsafe_surfaces(project_dir, "approve", inbox, signal)

    if evidence.strip():
        managed_write_api_text(
            evidence_file,
            f"# Run evidence — {todo_id}\n\n"
            f"_Recorded: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_\n\n"
            f"{evidence.strip()}\n",
            operation="write run evidence",
        )

    baseline_sha = _commit_plan.read_baseline_sha(project_dir, todo_id)
    # The candidate itself is NOT built yet (M2): a reject/conflict verdict
    # is resolved first (below), so a refused unit does not pay for the
    # mandatory check (a full test run in a throwaway worktree) only to
    # discard it.

    # SPEC A-run (second tier): record the verdict in the run diary.
    # AUD05-05 (rest): computed from the FRESH state inside the lock
    # (update_run), not from the snapshot read above. If a reject landed
    # first, this approve must NOT publish verdict='approved' — audit
    # invariant: 'approved' ⇒ rejects == 0. The reject's diary entry stays.
    # A-04 (audit 2026-09-25, layer 4): the APPROVE signal is published
    # ONLY after this decision succeeds. A conflict must refuse WITHOUT
    # the file — a leftover APPROVE signal would unlock the commit gate
    # on a rejected verdict.
    # ORCH M1.1: causal memory — the approve is recorded with a short
    # evidence excerpt (the full text stays in the RUN-EVIDENCE file). An
    # approve without a fresh evidence= but with an existing evidence file
    # (allowed: the file gate passes) gets the excerpt from the file.
    evidence_excerpt = ""
    if run_active:
        evidence_excerpt = evidence.strip()[:200]
        if not evidence_excerpt and evidence_file.is_file():
            try:
                evidence_excerpt = (
                    evidence_file.read_text(encoding="utf-8", errors="replace")
                    .strip()[:200]
                )
            except OSError:
                evidence_excerpt = ""

    conflict = False
    approved_generation: list[int] = [-1]
    if run_active:
        from .. import run_state as _run_state

        def _approve_mutator(state: dict) -> dict:
            nonlocal conflict
            if not state.get("active"):
                return state
            rejects_map = state.get("rejects") or {}
            if int(rejects_map.get(todo_id, 0) or 0) >= 1:
                conflict = True
                return state  # reject won — do not publish 'approved'
            outcomes = dict(state.get("outcomes") or {})
            outcomes[todo_id] = {"verdict": "approved"}
            state["outcomes"] = outcomes
            # M2.1: the run generation this verdict belongs to — captured
            # in the SAME lock hold as the verdict, so the binding stamps
            # the cycle the decision actually landed in.
            approved_generation[0] = _run_state.generation_of(state)
            # ORCH M1.1: the causal memory entry — same lock hold as the
            # verdict, dedup by (kind, todo_id, reason) inside the lock.
            _run_state.append_decision(state, "approve", todo_id, evidence_excerpt)
            return state

        _run_state.update_run(project_dir, _approve_mutator)

    if conflict:
        # Refused: no APPROVE signal, no VERIFIED fingerprint. The
        # reject's REVIEW file and diary entries are untouched. The stale
        # candidate evidence was already cleared on entry (A-04), so the
        # refusal cannot leave a file verify_pack would read as a valid
        # candidate for the rejected unit.
        return ApproveResult(
            todo_id=todo_id,
            signal_file="",
            evidence_file=str(evidence_file) if evidence.strip() else "",
            verified_sha_file="",
            message=(
                f"{todo_id}: a rejection already counted for this TODO — the "
                "verdict stays 'rejected' in the run diary (hard invariant: "
                "'approved' ⇒ rejects == 0). No APPROVE signal was created "
                "— the commit gate stays closed. Resolve the reject first."
            ),
        )

    # R2.4 (roadmap R2): the candidate is materialised and its mandatory
    # check runs HERE, past the reject/conflict verdict (M2) — a unit
    # already counted as rejected never pays for the check. A run-mode
    # approve with no baseline cannot define a candidate — the candidate
    # binding is skipped (the pre-R2.4 path); the M2.1 binding still
    # applies. With a baseline the candidate is mandatory.
    built = _candidate_verify.build_evidence(
        project_dir,
        todo_id,
        baseline_sha,
        confirm_degraded=confirm_degraded,
    ) if (run_active and baseline_sha) else None
    if built is not None and built.refusal:
        # A-04: the stale evidence was already cleared on entry; this
        # refusal writes nothing new.
        raise AwfApiError(
            f"candidate verification refused the approval for {todo_id}: "
            f"{built.refusal} No APPROVE signal, no verdict — fix the "
            "candidate (or confirm a legitimate degradation) and re-approve."
        )
    candidate_evidence = built.evidence if built is not None else None

    # R2.4 (F-QA-1): write the candidate evidence ONLY after the verdict
    # succeeded — a conflict-refused approve must leave no artefact behind
    # (A-04). The evidence is built above (it also feeds the APPROVE binding
    # below); the write is the side effect, and it belongs past the verdict.
    if candidate_evidence is not None:
        try:
            _candidate_verify.write_evidence(project_dir, candidate_evidence)
        except OSError as e:
            raise AwfApiError(
                f"candidate evidence could not be written for {todo_id}: {e}"
            ) from e

    # A-15: the fingerprint is written BEFORE the APPROVE signal becomes
    # visible. The commit gate enforces the file only while it exists, so
    # a gate that sees the signal must also see the fingerprint (atomic
    # rename); the old order left a window where the gate could start on
    # the bare signal and skip the re-check.
    if verified_fp:
        managed_write_api_text(
            verified_file,
            f"{verified_fp}\n",
            operation="write verified fingerprint",
        )

    managed_mkdir_api(inbox, operation=f"approve {todo_id}")
    if run_active:
        # M2.1: the approval is bound to the cycle it was made for — the
        # run generation captured under the verdict's lock, the verified
        # fingerprint, and the digest of the file set the commit gate
        # applies (plan_files on the gate's own baseline input). The
        # gate enforces the binding (commit_plan.binding_refusal): a
        # force-restarted run refuses the stale approval instead of
        # committing on it. Outside a run the signal stays an empty
        # marker (legacy behavior).
        from .. import run_state as _run_state

        gen = approved_generation[0]
        if gen < 0:
            # The run closed inside the window (verdict not written):
            # stamp the last known generation — the gate enforces the
            # binding only against an ACTIVE run, and a newer run that
            # starts before the commit fails closed on the mismatch.
            gen = _run_state.generation_of(run or {})
        if candidate_evidence is not None:
            # R2.4: the candidate evidence ALREADY carries the file-set
            # digest of the plan the supervisor checked — reuse it (one
            # assembly, not a second one next to the candidate).
            files_digest = candidate_evidence.files_digest
        else:
            files = _commit_plan.plan_files(project_dir, baseline_sha, todo_id)
            files_digest = _commit_plan.files_digest(files)
        binding = {
            "generation": gen,
            "verified_sha": verified_fp,
            "files_digest": files_digest,
        }
        if candidate_evidence is not None:
            # R2.4: the candidate binding — the gate re-derives these from
            # the current tree (candidate_refusal) and refuses on a change.
            # ONE schema (binding_projection), not a hand-copied list: a
            # field added to the evidence cannot desync the signal from its
            # evidence_digest.
            binding.update(candidate_evidence.binding_projection())
            binding["evidence_digest"] = candidate_evidence.evidence_digest()
        managed_write_api_text(  # approve binding
            signal,
            json.dumps(binding, sort_keys=True) + "\n",
            operation="write approve signal",
        )
    else:
        # K9: the marker is written no-follow too — a symlink swapped onto
        # the leaf between the preflight and the write is replaced, never
        # followed (the preflight already refused an existing one).
        managed_write_api_text(signal, "", operation="write approve signal")

    # RUN5 #1 (Part B): failsafe — list rejected-attempt files that would be
    # SILENTLY excluded from this commit (still untracked AND in the
    # baseline's untracked snapshot). Warning only: the approve is NOT
    # blocked and nothing is auto-committed — the supervisor re-issues with
    # carry_over_from or commits consciously.
    orphaned: list[str] = []
    try:
        from ..reject_files import orphaned_reject_files

        orphaned = sorted(
            {p for _origin, ps in orphaned_reject_files(project_dir, todo_id) for p in ps}
        )
    except Exception:
        orphaned = []  # best-effort failsafe — never blocks the approve

    # R2.2 QA iter 4: the paths are RAW (byte-faithful); escape them for the
    # result/JSON surface exactly like the dispatch transfer lists, so a name
    # with a non-UTF-8 byte never breaks the strict UTF-8 sink.
    from ..infra.git_model import escape_str_for_ui

    orphaned = [escape_str_for_ui(p) for p in orphaned]

    return ApproveResult(
        todo_id=todo_id,
        signal_file=str(signal),
        evidence_file=str(evidence_file) if evidence.strip() else "",
        verified_sha_file=str(verified_file) if verified_fp else "",
        message=(
            f"{todo_id}: a rejection already counted for this TODO — the "
            "verdict stays 'rejected' in the run diary (hard invariant: "
            "'approved' ⇒ rejects == 0). Resolve the reject first."
            if conflict
            else ""
        ),
        orphaned_files=orphaned,
    )


def reject_commit(project_dir: Path, todo_id: str, reason: str) -> RejectResult:
    """Reject work at verify — write REVIEW-{todo}.md; count in run mode.

    SPEC A-run.5: inside an active run the rejection increments
    ``rejects[todo]`` and is recorded in the run diary (``outcomes``).
    The SECOND rejection stops the run automatically with a report — the
    task itself needs the owner, another retry won't help. Outside a run
    this is just the REVIEW file (legacy behavior).
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(f"invalid todo_id '{todo_id}', expected format TODO-NNNN")
    if not reason or not reason.strip():
        raise AwfApiError("reason is required (what to fix)")
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    # ORCH M5.2 (review fix F1): a service-run unit is NOT in the main
    # queue — routing its reject through the generic path would count
    # rejects in the MAIN run and escalate a double-reject into stop_run
    # of the main run. Refused BEFORE any side effect (no REVIEW file, no
    # count, no escalation) — the verdict belongs to the service slot.
    if _is_service_unit(project_dir, todo_id):
        return RejectResult(
            todo_id=todo_id,
            review_file="",
            message=_service_unit_refusal(todo_id),
        )

    outbox = paths.outbox(project_dir)
    # REVIEW 0281 residual: the REVIEW artifact is a managed surface like
    # every other outbox writer — a bare ``mkdir``/write follows a
    # symlinked ``outbox`` or leaf and lands outside the project. Both the
    # directory and the leaf go through the no-follow barrier; a refusal
    # happens before the first effect (no file, no directory, no run
    # count).
    managed_mkdir_api(outbox, operation=f"reject {todo_id}")
    review_file = outbox / f"REVIEW-{todo_id}.md"
    managed_write_api_text(
        review_file,
        f"# REVIEW — {todo_id}\n\n## Reason\n{reason.strip()}\n",
        operation=f"reject {todo_id}",
    )

    # RUN5 #1 (Part A.1) + RB-2: record the attempt's files (new untracked
    # AND its tracked delta) so a re-dispatch can carry them over (leak-gate).
    # Best-effort by design — the snapshot never fails the reject, it logs
    # and returns [].
    from ..reject_files import snapshot_rejected_files

    rejected_files = snapshot_rejected_files(
        project_dir, todo_id, paths.logs_dir(project_dir)
    )

    from .. import run_state as _run_state

    run = _run_state.read_run(project_dir)
    rejects = 0
    run_stopped = False
    report_file = ""
    message = f"{todo_id} rejected — REVIEW written."
    if run and run.get("active"):
        # AUD05-05 (rest): count from the FRESH state inside the lock
        # (update_run). A reject is never lost: the counter always reads
        # what is on disk at the moment of the write, so a concurrent
        # approve/finish cannot blank the count that drives the
        # "rejected twice" gate.
        def _reject_mutator(state: dict) -> dict:
            nonlocal rejects
            if not state.get("active"):
                return state  # run closed meanwhile — stay legacy (no count)
            rejects_map = dict(state.get("rejects") or {})
            rejects = int(rejects_map.get(todo_id, 0) or 0) + 1
            rejects_map[todo_id] = rejects
            state["rejects"] = rejects_map
            outcomes = dict(state.get("outcomes") or {})
            outcomes[todo_id] = {
                "verdict": "rejected",
                "reason": reason.strip(),
                "rejects": rejects,
            }
            state["outcomes"] = outcomes
            # ORCH M1.1: the causal memory entry — the reject with its
            # reason, same lock hold as the counter (dedup inside the lock).
            _run_state.append_decision(state, "reject", todo_id, reason.strip())
            return state

        fresh = _run_state.update_run(project_dir, _reject_mutator)
        if rejects:
            message = f"{todo_id} rejected (rejection #{rejects} in this run)."
            if rejects >= 2:
                from .run import stop_run

                stopped = stop_run(
                    project_dir,
                    fresh,
                    f"{todo_id} rejected twice — the task needs the owner",
                )
                run_stopped = True
                report_file = stopped.report_file
                message += " Run STOPPED: two rejections — the task needs the owner."

    from ..infra.git_model import escape_str_for_ui

    return RejectResult(
        todo_id=todo_id,
        review_file=str(review_file),
        rejects=rejects,
        run_stopped=run_stopped,
        report_file=report_file,
        message=message,
        # R2.2 QA iter 4: RAW paths (byte-faithful) — escape for the result/JSON
        # surface, same as the dispatch transfer lists.
        reject_files=[escape_str_for_ui(p) for p in rejected_files],
    )


# ─── create_baseline ────────────────────────────────────────────────────


def _untracked_snapshot(project_dir: Path, excluded: set[str]) -> str:
    """The ``BASELINE-<todo>.untracked`` body: git's untracked listing minus
    the adopted paths, in the RAW identity of the R2.1 model.

    The format is the NUL machine protocol (``_path_link``): the paths are
    written with git's own bytes, so a name holding a newline, a control byte,
    an edge space or a non-UTF-8 byte is ONE path, not two lines. The adopted
    paths (``carry_over`` / ``include`` / ``claim``) are compared in the same
    raw identity, so the exclusion finally bites for a non-ASCII name.

    Migration (TODO-0228 review): files written before this change keep git's
    PRINTED form and the readers decode them lazily
    (``git_model.read_untracked_snapshot``). The model read carries the same
    triple guard as its siblings (M3): a hung/missing git
    (``SubprocessError``/``OSError``) or an unrepresentable tree degrades the
    snapshot to the legacy printed read, and any failure there to "" — the
    baseline write never crashes ``awf dispatch`` / ``awf run_next``.
    """
    from .._path_link import encode_path_link
    from ..infra.git_model import (
        GitModelError,
        GitRepository,
        display_path,
        raw_identity,
    )

    try:
        raw_paths = GitRepository(project_dir).untracked_paths()
    except (GitModelError, OSError):
        raw_paths = None
    if raw_paths is not None:
        return encode_path_link(
            [p for p in (display_path(p) for p in raw_paths) if p not in excluded]
        )
    try:
        printed = git_utils.git_stdout(
            project_dir, "ls-files", "--others", "--exclude-standard", check=False
        )
    except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
        return ""
    # The printed listing is newline-delimited — split on ``\n`` ONLY, as the
    # canonical legacy reader does: ``splitlines()`` would also cut on
    # U+2028/U+2029, so a raw name under ``core.quotePath=false`` would become
    # two fake paths (the T2 class, held here on the writer surface too).
    lines = [ln for ln in printed.split("\n") if ln]
    if excluded:
        lines = [ln for ln in lines if raw_identity(ln) not in excluded]
    return "\n".join(lines)


def create_baseline(
    project_dir: Path,
    todo_id: str,
    *,
    carry_over: set[str] | None = None,
    include: set[str] | None = None,
    claim: set[str] | None = None,
    test_timeout: float | None = None,
    run_tests: bool = True,
    timeout_status: str = "failed",
) -> BaselineResult:
    """Create BASELINE-{todo_id}.{sha,status,worktree,tests.log,env.log}
    snapshot.

    Captures git HEAD SHA, working tree status, test output, and Python
    environment. Used for A1 commit isolation and rollback targets.

    REPORTS33 (TODO-0204): when the tree is dirty, the worktree state is
    additionally snapshotted with ``git stash create`` (a commit object —
    the tree and the index are never touched) and its sha is written to
    ``BASELINE-{todo_id}.worktree``. The commit gate uses it to commit
    only the unit's own edit of a tracked-dirty file (the pre-dispatch
    change stays in the worktree). Empty ``stash create`` (no tracked
    changes) or a failure → no ``.worktree`` file, the dispatch warning
    degrades to the honest legacy text.

    ``carry_over`` (RUN5 #1, leak-gate): paths to EXCLUDE from the
    ``BASELINE-{todo_id}.untracked`` snapshot — files of a rejected attempt
    the caller deliberately re-claims, so the commit gate includes them in
    this unit's commit instead of treating them as pre-existing.

    ``include`` (RUN10 #4, TODO-0074): pre-existing untracked paths the
    caller deliberately re-claims into the unit's commit. Same mechanism as
    ``carry_over`` (excluded from the untracked snapshot) but a separate
    parameter — the two are independent mechanisms with different audit
    trails (BASELINE-{todo}.carry_over / BASELINE-{todo}.include).

    ``claim`` (R2.2, TODO-0221): paths of a predecessor unit's change that
    this unit deliberately owns via ``claim_changes`` — the CODE channel. The
    paths are excluded from the untracked snapshot exactly like ``include`` /
    ``carry_over`` (so the commit gate treats them as THIS unit's work). The
    source plan is the ``CLAIM-<source>.json`` record written by
    ``claim_changes.apply_claim_changes``; the re-baseline audit link is the
    third sibling, ``BASELINE-<todo>.claim`` (TODO-0221 M2 — written by
    ``claim_changes.write_claim_link`` at dispatch, re-applied by
    ``hygiene.transfer_exclusions``), kept parallel to the ``carry_over`` /
    ``include`` links above.

    ``test_timeout`` (TODO-0261, dispatch path): how long the baseline test
    command may run before its process group is killed. ``None`` keeps the
    legacy 300 s of the unit baseline. ``timeout_status`` names the status a
    timeout records — the dispatch passes ``"timeout"`` so the supervisor
    sees WHY the smoke snapshot is absent; the legacy path keeps ``"failed"``.
    ``run_tests=False`` skips the command entirely (``status="skipped"``) and
    only snapshots sha/status — ``awf dispatch --no-baseline-tests``.
    """
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(f"invalid todo_id '{todo_id}', expected format TODO-NNNN")
    # REPORTS26 B2: write entry point — a bare .agentic/ without
    # config.yaml (stale directory) is refused before any side effect.
    project_dir = require_awf_project(project_dir)

    context_dir = paths.context_dir(project_dir)
    # TODO-0235 (REVIEW B4): the baseline writes and removes files under
    # ``context/`` — one barrier for the surface before the first write, so a
    # symlinked context neither overwrites nor deletes outside the project.
    refuse_unsafe_surfaces(project_dir, "create baseline", context_dir)
    # REVIEW 0281 residual: the preflight above checks ``context`` on the
    # canonical (resolved-parent) basis; the directory and up to six baseline
    # files are then created through the raw-basis no-follow walk, so a link
    # swapped in after the preflight is refused before the first byte lands.
    # The two ``unlink`` removals below are a different branch: each keeps its
    # own ``refuse_unsafe_surfaces`` right before the delete.
    managed_mkdir_api(context_dir, operation="create baseline")

    # REPORTS26 B1: track the files THIS call writes — an unexpected
    # exception after the first write removes them before re-raise (zero
    # residue; a half-baseline would poison the next dispatch). A
    # successful baseline (incl. failed tests) keeps its files.
    written: list[Path] = []

    def _write_baseline_file(path: Path, content: str) -> None:
        # The untracked snapshot carries RAW path bytes (surrogateescape);
        # the other baseline files are ASCII. One writer, one codec — a
        # non-UTF-8 name must not crash the dispatch after the TODO is
        # reserved. The writer is the managed no-follow one (REVIEW 0281
        # residual): a symlinked component or leaf refuses instead of
        # writing outside the project.
        managed_write_api_text(
            path,
            content,
            operation="create baseline",
            errors="surrogateescape",
        )
        written.append(path)

    try:
        is_git = git_utils.is_git_repo(project_dir)
        if is_git:
            try:
                sha = git_utils.git_stdout(project_dir, "rev-parse", "HEAD").strip()
            except RuntimeError as e:
                # AUD05-08: `git init` with zero commits → `rev-parse HEAD`
                # fails. Surface a clean AwfApiError (MCP/CLI render it)
                # instead of letting the raw RuntimeError escape every caller.
                raise AwfApiError(
                    f"git repo at {project_dir} has no commits yet — create the "
                    "first commit, then retry. (git: "
                    f"{str(e).splitlines()[0] if str(e) else 'rev-parse HEAD failed'})"
                ) from e
            _write_baseline_file(context_dir / f"BASELINE-{todo_id}.sha", sha + "\n")
            # M3: the printed read decodes strictly (``text=True``); a
            # non-UTF-8 name under ``core.quotepath=false`` raises a
            # ``UnicodeDecodeError`` and a hung/missing git raises
            # ``SubprocessError``/``OSError``. U2 (TODO-0228): an EMPTY
            # snapshot would read as "the index was clean" and let a
            # pre-staged non-UTF-8 file enter the unit plan — a lie. Write
            # no file instead (the reader's documented legacy fallback:
            # ``staged_at_baseline`` → None, the whole staged-vs-baseline
            # set is treated as foreign), and drop any earlier dispatch's
            # file so a re-dispatch never reads it.
            try:
                status = git_utils.git_stdout(project_dir, "status", "--short", check=False)
            except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
                status = None
            status_path = context_dir / f"BASELINE-{todo_id}.status"
            if status is None:
                refuse_unsafe_surfaces(
                    project_dir, "create baseline", context_dir, status_path
                )
                status_path.unlink(missing_ok=True)
            else:
                _write_baseline_file(status_path, status)
            # REPORTS33 (TODO-0204): snapshot of the dirty worktree — the
            # commit gate's delta mechanism: a tracked-dirty file enters
            # the unit commit only as the unit's own edit (worktree vs
            # THIS snapshot); the pre-dispatch part stays in the worktree.
            # `git stash create` builds the commit object from the
            # worktree without touching tree or index (untracked files
            # are not part of it — they have their own QA-1 snapshot).
            # Written UNCONDITIONALLY (every other baseline file is): a
            # fresh sha on a dirty tree, the file REMOVED on empty output
            # (a clean tree, or a swallowed `stash create` failure — both
            # read as "") — a re-dispatch of the same id must never read
            # an earlier dispatch's snapshot (QA F6, REVIEW iter 5). Best
            # effort either way: a failure never fails the baseline (the
            # dispatch warning degrades to the honest legacy text). The
            # ``check=False`` covers git's own NON-ZERO rc; a TRANSIENT failure
            # (hung/missing git) raises out of ``_git`` — the same triple guard
            # M3 gave the sibling status / untracked reads above, or the
            # exception escapes the outer ``except Exception: ... raise`` and
            # crashes ``awf dispatch`` / ``awf run_next``.
            try:
                worktree_snap = git_utils.git_stdout(
                    project_dir, "stash", "create", check=False,
                ).strip()
            except (subprocess.SubprocessError, OSError, UnicodeDecodeError):
                worktree_snap = ""
            snap_path = context_dir / f"BASELINE-{todo_id}.worktree"
            if worktree_snap:
                _write_baseline_file(snap_path, worktree_snap + "\n")
            else:
                refuse_unsafe_surfaces(
                    project_dir, "create baseline", context_dir, snap_path
                )
                snap_path.unlink(missing_ok=True)
            # QA-1: snapshot untracked files at baseline time so commit_gate
            # can distinguish worker-created files from pre-existing untracked.
            # RUN5 #1 (leak-gate) + RUN10 #4 (TODO-0074): drop re-claimed
            # paths (carry-over and/or include) from the snapshot so the
            # commit gate treats them as THIS unit's work (it would
            # otherwise exclude them as "pre-existing" and silently lose
            # them from the unit commit). The exclusion compares RAW path
            # identities, so a non-ASCII adopted name is dropped too.
            excluded = set(carry_over or ()) | set(include or ()) | set(claim or ())
            untracked = _untracked_snapshot(project_dir, excluded)
            _write_baseline_file(context_dir / f"BASELINE-{todo_id}.untracked", untracked)
        else:
            sha = "(not a git repo)"
            _write_baseline_file(context_dir / f"BASELINE-{todo_id}.sha", sha + "\n")
            _write_baseline_file(context_dir / f"BASELINE-{todo_id}.status", "")

        config_file = paths.config_file(project_dir)
        tests_log_path = context_dir / f"BASELINE-{todo_id}.tests.log"
        test_status = "no_config"
        test_log_excerpt = ""
        effective_timeout = 300 if test_timeout is None else max(0.0, float(test_timeout))

        if not run_tests:
            _write_baseline_file(
                tests_log_path,
                "baseline tests skipped (run_tests=False)\n",
            )
            test_status = "skipped"
        elif config_file.exists():
            config_data = cfg_mod.load(project_dir)
            # U1: dispatch-time baseline runs a fast smoke command (baseline_cmd)
            # instead of the full test_cmd. Absent/empty/blank → legacy test_cmd.
            baseline_cmd = (
                cfg_mod.get(config_data, "verification.baseline_cmd", "") or ""
            ).strip()
            test_cmd = cfg_mod.get(config_data, "verification.test_cmd", "") or ""
            run_cmd = baseline_cmd or test_cmd
            cmd_key = "baseline_cmd" if baseline_cmd else "test_cmd"
            if run_cmd.strip():
                # I1 (REVIEW виток 6) + architect-review M8: the config
                # commands are the SAME argv class as the contract's
                # `verify:` — one parse rule (`verify.parse_argv`: shlex +
                # the A4 operator refusal), so `test_cmd` means the same
                # thing here, in verify and in the candidate check. A blank
                # command stays "not configured" (the legacy skip), never
                # an "empty command" refusal.
                parts, problem = parse_argv(run_cmd)
                if problem:
                    # AUD14-02: bad quoting must not traceback baseline
                    raise AwfApiError(
                        f"verification.{cmd_key}: {problem} (cmd: {run_cmd!r})"
                    )
                try:
                    result = run_tree(
                        parts,
                        cwd=str(project_dir),
                        capture_output=True,
                        text=True,
                        timeout=effective_timeout,
                    )
                except subprocess.TimeoutExpired:
                    # cmd hung (watcher / stdin prompt / infinite loop).
                    # Don't block baseline creation — record failure, continue.
                    _write_baseline_file(
                        tests_log_path,
                        f"{cmd_key} timed out after {effective_timeout:g}s: "
                        f"{run_cmd}\n",
                    )
                    test_status = timeout_status
                    test_log_excerpt = f"{cmd_key} timed out: {run_cmd}"
                except FileNotFoundError:
                    # REPORTS26 B1: bare binary not in PATH (e.g. `vitest`
                    # stored from package.json scripts). Same contract as
                    # the timeout: record failure, continue — dispatch
                    # must not die on a broken command.
                    _write_baseline_file(
                        tests_log_path,
                        f"{cmd_key} не найдена в PATH: {run_cmd}. "
                        "Поправьте verification.baseline_cmd/test_cmd\n",
                    )
                    test_status = "failed"
                    test_log_excerpt = f"{cmd_key} не найдена в PATH: {run_cmd}"
                except OSError as e:
                    # architect-review M8: the argv launcher (no shell) raises
                    # for a non-executable command / bad interpreter, where the
                    # old shell reported rc 126. Same contract as the missing
                    # binary — one typed "record failure, keep going" outcome,
                    # never a traceback out of dispatch. The other argv
                    # surfaces (verify.run_verify_commands, verify_pack._run_cmd,
                    # candidate_verify.run_check) already catch OSError.
                    _write_baseline_file(
                        tests_log_path,
                        f"{cmd_key} не запускается: {e}\n",
                    )
                    test_status = "failed"
                    test_log_excerpt = f"{cmd_key} не запускается: {e}"
                else:
                    log_content = result.stdout + result.stderr
                    _write_baseline_file(tests_log_path, log_content)
                    test_status = "passed" if result.returncode == 0 else "failed"
                    test_log_excerpt = "\n".join(log_content.splitlines()[-5:])
            else:
                _write_baseline_file(tests_log_path, "No test_cmd configured, skipping test baseline.\n")
                test_status = "no_test_cmd"
        else:
            _write_baseline_file(tests_log_path, "No config.yaml found, skipping test baseline.\n")
            test_status = "no_config"

        python_cmd = "python3"
        if not shutil.which("python3") and shutil.which("python"):
            python_cmd = "python"

        env_parts: list[str] = []
        for cmd in [[python_cmd, "--version"], [python_cmd, "-m", "pip", "list"]]:
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                env_parts.append(result.stdout + result.stderr)
            except subprocess.TimeoutExpired:
                env_parts.append(f"{cmd[0]} timed out after 30s\n")
            except (FileNotFoundError, OSError) as e:
                env_parts.append(f"{cmd[0]} failed: {e}\n")
        _write_baseline_file(context_dir / f"BASELINE-{todo_id}.env.log", "".join(env_parts))

        # AUD-0282-3: report only the files THIS call wrote — the glob also
        # picked up a stale ``BASELINE-<id>.claim/.include/.carry_over`` or a
        # sibling snapshot, so the dispatch answer named files it never made.
        files_created = sorted({path.name for path in written})

        return BaselineResult(
            todo_id=todo_id,
            sha=sha,
            resolved_project_dir=str(project_dir),
            is_git_repo=is_git,
            files_created=files_created,
            test_status=test_status,
            test_log_excerpt=test_log_excerpt,
        )
    except Exception:
        for path in written:
            try:
                refuse_unsafe_surfaces(
                    project_dir, "create baseline cleanup", context_dir, path
                )
                path.unlink(missing_ok=True)
            except (OSError, AwfApiError):
                pass
        raise


# ─── rollback ───────────────────────────────────────────────────────────


def rollback(
    project_dir: Path,
    todo_id: str,
    *,
    mode: str = "hard",
    confirm_digest: str | None = None,
) -> RollbackResult:
    """Rollback project to BASELINE-{todo_id}.sha.

    R1.4 (требование 4): разрушительные режимы (hard/soft) держат ту же
    критическую секцию, что и запуск, — start не может опубликовать
    заявку и стартовать между preflight и ``git reset``. Занятая секция
    — отказ, не ожидание. ``dry-run`` только читает и секцию не берёт.

    R1.4 (F-R2): ``confirm_digest`` — digest плана из
    :func:`awf.api.preview_destruction` (kind ``rollback``); при
    несовпадении (в т.ч. устаревшем плане) rollback отвергается до
    ``git reset``. ``None`` — legacy-путь. ``dry-run`` секцию не берёт, но
    ``confirm_digest`` для него не требуется (он ничего не меняет).

    Modes: ``hard`` (default, ``git reset --hard``), ``soft`` (``git reset``,
    keeps working tree), ``dry-run`` (returns diff without modifying).
    """
    resolved = Path(project_dir).resolve()
    # dry-run only reads — it never takes the section.
    if mode not in ("hard", "soft"):
        return _rollback_impl(
            resolved, todo_id, mode=mode, confirm_digest=confirm_digest
        )
    from ._interlock import destructive_section

    # A project without ``.agentic`` yields free (see ``destructive_section``);
    # ``_rollback_impl`` raises ``require_agentic`` exactly as it did before.
    with destructive_section(resolved, label=f"rollback({mode})"):
        return _rollback_impl(
            resolved, todo_id, mode=mode, confirm_digest=confirm_digest
        )


def _rollback_diff_stat(project_dir: Path, baseline_sha: str) -> str:
    """``git diff --stat`` for the rollback summary — decoded STRICTLY.

    The summary is display text, so a raw non-UTF-8 name (``core.quotePath``
    off) must surface as a refusal, never as a silently aliased path
    (``single-owners.md``). Both failures below are translated into the
    API's refusal type — a decode error escaping as a traceback was the
    remaining gap from the M4 audit.
    """
    try:
        result = subprocess.run(
            ["git", "diff", baseline_sha, "--stat"],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            check=False,
            # FU-19 (AUD01-02 tail): the last unbounded git call — contract
            # docs/contracts/subprocess-timeouts.md.
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        raise AwfApiError(
            f"git diff timed out after {e.timeout}s — a hung git call blocks "
            "the rollback. Check the repo state (index.lock, huge diff) and retry."
        ) from e
    except UnicodeDecodeError as e:
        raise AwfApiError(
            "git diff --stat output is not valid UTF-8 — the rollback summary "
            "cannot be rendered. Nothing was reset; rename the offending "
            "path or commit it and retry."
        ) from e
    return result.stdout


def _rollback_impl(
    project_dir: Path,
    todo_id: str,
    *,
    mode: str = "hard",
    confirm_digest: str | None = None,
) -> RollbackResult:
    if not todo_id:
        raise AwfApiError("todo_id is required")
    if not re.match(r"^TODO-\d{4,}$", todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}', expected format TODO-NNNN"
        )
    if mode not in ("hard", "soft", "dry-run"):
        raise AwfApiError(f"invalid mode '{mode}', expected hard/soft/dry-run")

    project_dir = Path(project_dir).resolve()

    # R1.4 (S9-02): a destructive rollback (hard/soft moves HEAD) must not
    # race a live launch — same preflight facts as reset, one refusal form.
    # dry-run is read-only and skips it.
    if mode in ("hard", "soft"):
        from ._liveness import refuse_if_in_use

        refuse_if_in_use(
            project_dir, label=f"rollback({mode})", action="write"
        )

    # SPEC A-run.4 (stop-list): destructive rollback is an owner decision.
    # While a run is active, `hard` is refused — stop the run first.
    if mode == "hard":
        from .. import run_state as _run_state

        run = _run_state.read_run(project_dir)
        if run and run.get("active"):
            raise AwfApiError(
                "Run is active — rollback(hard) is a stop-list item (owner decision). "
                "Stop the run first (awf_run_finish) and ask the owner, or use mode='soft' "
                "to keep a way back."
            )

    require_agentic(project_dir)
    context_dir = paths.context_dir(project_dir)
    inbox = paths.inbox(project_dir)
    baseline_sha_file = context_dir / f"BASELINE-{todo_id}.sha"

    # AUD1 (REVIEW 0281): the rollback writes TWO surfaces after the reset —
    # ``context`` (``forget_unit_history`` unlinks CLOSER/CHECKLIST) and
    # ``inbox`` (the ACK). A symlinked surface would send those writes
    # outside the project; the preflight is the same barrier as restore and
    # the archive, run BEFORE the baseline read and the git reset, so a
    # refusal leaves the repo untouched. ``dry-run`` writes nothing and
    # keeps its read-only behaviour.
    if mode != "dry-run":
        refuse_unsafe_surfaces(
            project_dir,
            f"rollback({mode})",
            context_dir,
            inbox,
            baseline_sha_file,
            inbox / f"ACK-{todo_id}.ready",
        )

    if not baseline_sha_file.exists():
        raise AwfApiError(
            f"Baseline SHA not found: {baseline_sha_file}. "
            "Cannot rollback without baseline."
        )

    baseline_sha = baseline_sha_file.read_text(encoding="utf-8").strip()
    # AUD12-11: garbage in BASELINE-{todo}.sha used to surface as a raw
    # CalledProcessError from `git reset`. Validate the shape first — the
    # message then names the file to check instead of git's argv noise.
    if not re.fullmatch(r"[0-9a-f]{7,40}", baseline_sha):
        raise AwfApiError(
            f"baseline SHA {baseline_sha!r} is not a valid git revision — "
            f"{baseline_sha_file} is corrupt. Recreate the baseline "
            f"(awf_baseline {todo_id}) and retry."
        )

    # F-R2: confirm the plan (under the section) before the git reset.
    if mode in ("hard", "soft"):
        from . import _destructive

        _destructive.verify_confirm(
            project_dir,
            "rollback",
            confirm_digest,
            mode=mode,
            baseline_sha=baseline_sha,
            todo_id=todo_id,
        )

    diff_stat = _rollback_diff_stat(project_dir, baseline_sha)

    if mode == "dry-run":
        return RollbackResult(
            todo_id=todo_id,
            baseline_sha=baseline_sha,
            mode=mode,
            ack_file=None,
            diff_stat=diff_stat,
        )

    git_flag = "--hard" if mode == "hard" else ""
    git_cmd = ["git", "reset"]
    if git_flag:
        git_cmd.append(git_flag)
    git_cmd.append(baseline_sha)
    try:
        # AUD12-11: capture git's stderr — a rejected revision must surface
        # as the AwfApiError message below, not as raw `fatal:` noise on
        # the human's terminal.
        subprocess.run(
            git_cmd,
            cwd=str(project_dir),
            check=True,
            timeout=30,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as e:
        raise AwfApiError(
            f"git reset timed out after {e.timeout}s — a hung git call leaves "
            "the repo in an unknown state. Check the repo (index.lock) and retry."
        ) from e
    except subprocess.CalledProcessError as e:
        # AUD12-11: well-formed SHA that git still rejects (deleted branch,
        # shallow clone without that commit) — typed error, not traceback.
        raise AwfApiError(
            f"git reset to baseline {baseline_sha[:12]} failed (exit "
            f"{e.returncode}) — the revision is not reachable in this repo. "
            f"Check {baseline_sha_file} and the git history."
        ) from e

    # CTX2: the rollback re-runs the same unit id — its unkeyed history
    # (CLOSER first verdict/attempts, CHECKLIST verdict cache) would be
    # inherited by that re-run. Forgotten on the main path too, not only
    # on the service rollback (one home: awf.unit_history).
    forget_unit_history(project_dir, todo_id)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ack_content = (
        f"signal: TASK_ACK\n"
        f"ack_type: DONE\n"
        f"decision: rollback\n"
        f"referenced_task_id: {todo_id}\n"
        f"baseline_sha: {baseline_sha}\n"
        f"created_by: supervisor\n"
        f"created_at: {ts}\n"
    )
    ack_path = inbox / f"ACK-{todo_id}.ready"
    # AUD1: no-follow write, not a bare atomic_write — a link swapped onto
    # the ACK leaf between the preflight and here is replaced, never
    # followed outside the inbox.
    managed_write_api_text(
        ack_path, ack_content, operation=f"rollback({mode}) ack"
    )

    return RollbackResult(
        todo_id=todo_id,
        baseline_sha=baseline_sha,
        mode=mode,
        ack_file=str(ack_path),
        diff_stat=diff_stat,
    )


# ─── start_pipeline / continue_pipeline ─────────────────────────────────


def _stage_timeout_refusal(project_dir: Path) -> StartResult | None:
    """OPS IMP1 (R1/INV8): a bad project cap refuses before any file.

    The caller value is a raise (``stage_timeout.validate_caller``); the
    project key is a state refusal — same family as "another launch is in
    progress", rc 1, nothing written. The message wording lives in
    ``stage_timeout.project_refusal`` (shared with ``run_next``).
    """
    message = stage_timeout.project_refusal(cfg_mod.load(project_dir))
    if message is None:
        return None
    return StartResult(
        run_mode="noop",
        run_id=None,
        log_file=None,
        exit_code=1,
        message=message,
    )


def start_pipeline(
    project_dir: Path,
    *,
    background: bool = False,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int | None = None,
    todo_id: str = "",
    no_checkpoints: bool = False,
) -> StartResult:
    """Start the pipeline from the beginning.

    OPS IMP6 (R6/K7): the answer carries the launch handshake (runner/plugin
    versions + advisory drift warning) on every path — applied by this
    wrapper, so the warning never changes the outcome (INV7).

    ``todo_id`` pins the pipeline to a specific TODO (NEG-2026-09-19 R1):
    without it the engine picks the "newest active" TODO, which breaks run
    queues with dependencies.

    ``background=True`` launches a detached subprocess and returns immediately
    with a PID. Otherwise runs synchronously and returns the final exit code.

    ``no_checkpoints`` (RUN3 #6): skip the BD-36 plan checkpoint form for
    THIS launch only. Process-scoped (env for the duration of the pipeline
    process) — not written to config or state, the next launch behaves as
    before. The run-level flag (``run_start(no_checkpoints=...)``) and the
    config/env bypasses keep working; any one of them is enough.
    """
    return apply_handshake(
        _start_pipeline(
            project_dir,
            background=background,
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
            todo_id=todo_id,
            no_checkpoints=no_checkpoints,
        ),
        project_dir,
    )


def _start_pipeline(
    project_dir: Path,
    *,
    background: bool = False,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int | None = None,
    todo_id: str = "",
    no_checkpoints: bool = False,
) -> StartResult:
    """The launch itself; :func:`start_pipeline` adds the handshake fields."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    # OPS IMP1 (R1/INV8): validate the cap BEFORE the readiness gate, the
    # lease and the first file — caller by raise, project key as a refusal.
    timeout = stage_timeout.validate_caller(timeout)
    timeout_refusal = _stage_timeout_refusal(project_dir)
    if timeout_refusal is not None:
        return timeout_refusal

    # SPEC IMP3 (K3): the readiness gate for the selected unit, BEFORE the
    # lease, the PID file and the first stage. A READY package is required
    # only when the unit declares ``spec_package``; everything else keeps
    # the pre-unit behavior.
    launch_todo = (todo_id or "").strip() or (todos.newest_active(project_dir) or "")
    if launch_todo and not is_valid_todo_id(launch_todo):
        return _invalid_todo_refusal("start", launch_todo)
    gate_message = _spec_gate_for_launch(project_dir, launch_todo, pipeline or "")
    if gate_message:
        return _spec_gate_refusal(gate_message)

    # TODO-0262 REVIEW (A1): the review-pass budget is a LAUNCH gate too.
    # Without it ``awf start`` on an exhausted unit runs plan/implementer/
    # arch/qa/audit and only then dies at verify — a full round paid for
    # nothing. Check BEFORE the reconcile, the lease and the first stage:
    # an exhausted unit leaves the BUDGET note and starts no stage.
    if launch_todo:
        refusal = _budget_refusal(project_dir, launch_todo, "start")
        if refusal is not None:
            return refusal

    # DF6-2: reconcile state before starting — archive completed TODOs,
    # clear stale PIDs, deduplicate signals. Only for background mode
    # (MCP/supervisor path). Foreground/CI tests may pre-create APPROVE
    # signals that should not trigger archival.
    if background:
        _reconcile(project_dir)

    # Dogfood-10: guard — refuse to start pipeline without active TODO.
    # Only for background mode (production supervisor flow). Foreground
    # mode is used by tests/CI — skip guard there.
    if background and not from_stage:
        active = todo_id or todos.newest_active(project_dir)
        if not active:
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=(
                    "No active TODO in inbox. Pipeline needs a TODO to work on. "
                    "Create one via awf_dispatch_todo(project_dir, content, role), "
                    "then call awf_start again."
                ),
            )

    # A-02: launch lease — the single owner of the window "liveness check
    # → spawn → PID file write". A concurrent call while a live launch is
    # in progress is refused (non-blocking, not a wait); a dead owner's
    # lease is taken over automatically (staleness = process liveness,
    # never file age).
    # TODO-0118: the lease is taken in foreground too and held for the
    # whole run — a concurrent foreground call is refused the same way, so
    # a foreground pair also gives exactly one running pipeline. The
    # background child (the pipeline itself, spawned by the launch that
    # owned the lease) must not be refused by its own launch.
    is_bg_child = _is_background_child()
    # R1.4 (F-R1, требование 2): the launched child carries the ExecutionId
    # of its launch. Before doing ANY work it confirms the current launch
    # reservation still names it — a crash-orphan child (its launch is gone,
    # a newer launch owns the project) exits without work instead of running
    # a second pipeline over the live project.
    if is_bg_child and not _lease.execution_confirmed(
        project_dir, os.environ.get(_lease.EXECUTION_ENV, "")
    ):
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message=(
                "launch aborted — this child's ExecutionId is not the current "
                "launch reservation (a stale / crash-orphan child of an "
                "earlier launch); no work performed."
            ),
        )
    lease = None
    if not is_bg_child:
        try:
            # F-ID: publish_identity=False — the marker is written only
            # after the liveness gate below proves this launch proceeds, so
            # a refused start never rewrites a live child's ExecutionId.
            lease = _lease.acquire(project_dir, publish_identity=False)
        except _lease.LeaseHeldError as e:
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=f"Launch refused: {e}",
            )

    # F-1 (REVIEW A-02): everything after acquire runs under ONE try/finally
    # — any error in the window (liveness, config, closure resolution,
    # spawn) releases the lease, so a failed call can never strand a
    # live-pid lease and block the project until the caller process dies.
    # release() is idempotent and ownership-checked — one unconditional
    # release in finally covers every path (success, refusal, error).
    try:
        # DF5-6 + AUD04-07: refuse to start if a pipeline is already running
        # (shared resolver: background PID file AND state.pipeline_pid).
        # TODO-0118: refusal in foreground too — the mixed pair (background
        # running, foreground call arrives) is refused by the shared
        # resolver instead of running a second pipeline. The background
        # child is exempt: its own PID file is itself, not a running
        # pipeline.
        live_pid = _liveness.resolve(project_dir)[1]
        if live_pid and not is_bg_child:
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=(
                    f"Pipeline already running (PID {live_pid}). "
                    f"Use awf_status to check progress, or kill PID {live_pid} to force restart."
                ),
            )

        # F-ID (REVIEW-фикс 12): this launch passed the liveness gate and
        # WILL proceed — only now rotate the identity marker. It must
        # happen before start_in_background reads it for the child's env.
        if lease is not None:
            lease.publish_identity()

        # Dogfood-8: detect BD-36 checkpoint state + build appropriate warning
        from ..plan_checkpoint import is_checkpoint_enabled

        config_data = cfg_mod.load(project_dir)
        # RUN3 #6: with the launch parameter the checkpoint is off for this run,
        # so the foreground incompatibility guard below must not refuse it.
        checkpoint_active = is_checkpoint_enabled(
            config_data, auto, no_checkpoints=no_checkpoints
        )

        # RUN8 #2: a worker that outlived a previous kill may still be writing
        # — the new unit's commit would absorb its edits. Say so in the answer.
        orphan_warn = orphan_worker_warning(project_dir)

        # TODO-0197 (REPORTS32 п.3): the background launch will open the
        # BD-36 plan checkpoint form at the plan stage — name it in the
        # answer (a single awf_start used to open the form silently).
        # checkpoint_active already folds in no_checkpoints/auto/env/config.
        checkpoint_warn = ""
        if background and checkpoint_active:
            checkpoint_warn = (
                "A planned BD-36 checkpoint form will open at the plan "
                "stage — pass no_checkpoints=true to skip it."
            )

        if background:
            # A-02: the PID file is written inside start_in_background, so
            # the shared liveness resolver now sees the child.
            pid, log_file, _pid_file = start_in_background(
                project_dir,
                pipeline=pipeline,
                from_stage=from_stage,
                auto=auto,
                timeout=timeout,
                todo_id=todo_id,
                no_checkpoints=no_checkpoints,
            )

            # DF5-10: wait briefly, then check if child died immediately.
            child_alive = _verify_child_alive(pid, log_file)

            if not child_alive:
                log_tail = ""
                try:
                    log_tail = log_file.read_text(encoding="utf-8")[-500:] if log_file else ""
                except OSError:
                    pass
                return StartResult(
                    run_mode="error",
                    run_id=pid,
                    log_file=str(log_file) if log_file else None,
                    exit_code=1,
                    message=(
                        f"Pipeline started (PID {pid}) but exited immediately. "
                        f"Last log output:\n{log_tail}"
                    ),
                )

            # SMO: message tells supervisor to GO IDLE (not poll).
            # Dogfood #4: old message said "call awf_wait_for_event" → model polled.
            msg = f"awf start running in background (PID {pid}). GO IDLE — wait for user."
            if orphan_warn:
                msg = orphan_warn + " " + msg
            return StartResult(
                run_mode="background",
                run_id=pid,
                log_file=str(log_file),
                exit_code=None,
                message=msg,
                checkpoint_warning=checkpoint_warn,
            )

        # Dogfood-8: foreground + checkpoint enabled = incompatible
        # DF6-5: EXCEPT when this process is a background child (AWF_BACKGROUND_CHILD=1).
        # In background mode, stdout goes to a log file, not MCP stdio — so the
        # checkpoint form can safely open in browser without conflicts.
        if checkpoint_active and not os.environ.get("AWF_BACKGROUND_CHILD"):
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=1,
                message=(
                    "Foreground mode incompatible with BD-36 interactive checkpoint "
                    "(stdout conflicts with MCP stdio, form won't display). "
                    "Use background=True (default) or disable checkpoint via "
                    "AWF_PLAN_CHECKPOINT=false env var."
                ),
            )

        from ..orchestrator import run_pipeline

        args = PipelineArgs(
            project_dir=str(project_dir),
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
            todo_id=todo_id,
            no_checkpoints=no_checkpoints,
        )
        try:
            exit_code = run_pipeline(args)
        except Exception as e:
            return StartResult(
                run_mode="foreground",
                run_id=None,
                log_file=None,
                exit_code=1,
                message=f"Pipeline crashed: {e}\n{traceback.format_exc()}",
            )
        message = f"Pipeline completed with exit code {exit_code}"
        if orphan_warn:
            message = orphan_warn + " " + message
        return StartResult(
            run_mode="foreground",
            run_id=None,
            log_file=None,
            exit_code=exit_code,
            message=message,
        )
    finally:
        if lease is not None:
            lease.release()


def continue_pipeline(
    project_dir: Path,
    *,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int | None = None,
    background: bool = True,
    ack: str = "",
    no_checkpoints: bool = False,
    todo_id: str = "",
) -> StartResult:
    """Resume an interrupted pipeline, pinned to the right unit.

    Unit resolution: explicit ``todo_id`` > ``ack`` > state-file todo_id >
    "newest active" TODO. The answer names the unit it resumes.

    DF5-2: reads ``pipeline_state`` to determine which stage to resume from.
    If state file has ``stage_name``, uses it as ``from_stage`` — so the
    pipeline resumes from the stage that was running when it stopped,
    NOT from stage 0 (plan).

    ``todo_id``: explicit pin — the TODO to resume, winning over the ack,
    the state-file id and the "newest active" heuristic. ``retry_stage``
    uses it to restart exactly the salvaged unit (AUD08-02 class, RUN5
    incident 2026-09-22).

    ``ack`` (RUN8 #1): the supervisor's answer to a specific unit — it PINS
    exactly that unit (topic-trainer incident 2026-09-23: an ack for
    TODO-0034 restarted the newest active TODO-0037 and stranded the ACK
    as a stale closure). The ACK is consumed on the way in, so no stale
    closure is left. An explicit ``todo_id`` for another unit wins the pin
    and the ack is not written (the answer says so); a missing TODO file
    or a completed (DONE) unit is a clear refusal — no ACK is written.

    ``background=True`` (default) launches a detached subprocess — same
    infrastructure as ``start_pipeline``. Without this, MCP tool would
    block the event loop for the entire pipeline duration.

    Day-2 B3: interactive supervisor answers must survive process death.
    An ``ACK``/``APPROVE`` sitting in the inbox (written after the process
    died) is consumed here and the TODO is resumed — previously such a TODO
    was "closed" by the signal and `awf continue` answered "No active TODO
    found". ``ack="TODO-NNNN"`` writes the ACK for a blocked TODO before
    resuming (no manual ``touch`` needed).

    ``no_checkpoints`` (RUN3 #6): skip the BD-36 plan checkpoint form for
    THIS launch only — process-scoped, not written to config or state
    (same semantics as :func:`start_pipeline`).
    """
    return apply_handshake(
        _continue_pipeline(
            project_dir,
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
            background=background,
            ack=ack,
            no_checkpoints=no_checkpoints,
            todo_id=todo_id,
        ),
        project_dir,
    )


def _continue_pipeline(
    project_dir: Path,
    *,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int | None = None,
    background: bool = True,
    ack: str = "",
    no_checkpoints: bool = False,
    todo_id: str = "",
) -> StartResult:
    """The resume itself; :func:`continue_pipeline` adds the handshake fields."""
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    # OPS IMP1 (R1/INV8): same early cap validation as start_pipeline —
    # before the ACK is written, the lease is taken and a stage starts.
    timeout = stage_timeout.validate_caller(timeout)
    timeout_refusal = _stage_timeout_refusal(project_dir)
    if timeout_refusal is not None:
        return timeout_refusal

    if ack and not re.match(r"^TODO-\d{4,}$", ack):
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            # AUD07-01: user-caused refusal (typo in --ack) — non-zero so
            # `set -e` scripts catch it. State-condition noops (no active
            # TODO, pipeline already running) stay 0.
            exit_code=1,
            message=f"Invalid TODO id for --ack: {ack!r} (expected TODO-NNNN).",
        )

    # RUN8 #1 (TODO-0063): an ack is the supervisor's answer to a specific
    # unit — it pins exactly that unit. Validate the target BEFORE writing
    # the signal: a stranded ACK is the stale-closure bug this fixes
    # (topic-trainer incident 2026-09-23).
    explicit_pin = (todo_id or "").strip()
    ack_note = ""

    # SPEC IMP3 (K3): the same readiness gate BEFORE the ACK is armed, the
    # lease is taken and a stage is started. The resumed unit is resolved by
    # the SAME helper the launch below uses — the gate never checks another
    # unit than the one that starts.
    resume_target = _resume_target(project_dir, explicit_pin, ack or "")
    if resume_target and not is_valid_todo_id(resume_target):
        return _invalid_todo_refusal("continue", resume_target)
    gate_message = _spec_gate_for_launch(project_dir, resume_target, pipeline or "")
    if gate_message:
        return _spec_gate_refusal(gate_message)

    # TODO-0262: resuming the implementer stage of a unit whose review-pass
    # budget is exhausted would be another polish round — refuse, leave the
    # BUDGET note and let the owner decide. The state noop (no launch, no
    # signal, no lease) is the family of "another launch is in progress".
    # The effective stage includes the state-recorded one: a bare ``awf
    # continue`` names no stage yet resumes the interrupted implementer.
    resume_stage = _resume_stage(from_stage, read_state(project_dir))
    if "implement" in resume_stage.lower() and resume_target:
        refusal = _budget_refusal(project_dir, resume_target, "continue")
        if refusal is not None:
            return refusal

    if ack:
        if explicit_pin and explicit_pin != ack:
            # The explicit pin wins — but the ack answers ANOTHER unit;
            # writing its ACK would strand a closure for that unit, so the
            # signal is skipped and the answer says so.
            ack_note = f"(ack {ack} not applied — the explicit pin {explicit_pin} wins)"
        else:
            # The SAME two predicates the consumer and the noop hint use
            # (``_unit_file_present`` / ``_unit_cycle_done``) — kept apart
            # only because each refusal owns its own diagnostic text.
            ack_inbox = paths.inbox(project_dir)
            if not _unit_file_present(ack_inbox, ack):
                return StartResult(
                    run_mode="noop",
                    run_id=None,
                    log_file=None,
                    exit_code=1,
                    message=(
                        f"ack targets {ack}, but there is no TODO file in the "
                        f"inbox — nothing to accept. Check the id; an archived "
                        f"unit comes back via awf_restore."
                    ),
                )
            if _unit_cycle_done(paths.outbox(project_dir), ack):
                return StartResult(
                    run_mode="noop",
                    run_id=None,
                    log_file=None,
                    exit_code=1,
                    message=(
                        f"{ack} is already completed (DONE in the outbox) — "
                        f"nothing to accept."
                    ),
                )
            _arm_ack_file(ack_inbox, ack)

    # M2 (REVIEW виток 10): a pin that won over the ack means the ack was
    # never written — the post-reconcile re-arm must skip it.
    pin_overridden = bool(ack) and bool(explicit_pin) and explicit_pin != ack
    # A-02: launch lease (same as start_pipeline) — one owner of the
    # window "liveness check → spawn → PID file write". Acquired AFTER the
    # ack block on purpose: an ack that gets refused by a concurrent
    # launch stays in the inbox as the supervisor's pending answer (the
    # next continue consumes it) instead of being dropped.
    # TODO-0118: taken in foreground too — a concurrent foreground
    # continue is refused the same way (exactly one running pipeline).
    # The background child re-enters start_pipeline, not this function,
    # so no child exception is needed here.
    lease = None
    try:
        # F-ID: publish_identity=False — see start_pipeline: the marker is
        # written after the liveness gate so a refused continue does not
        # rewrite a live child's ExecutionId.
        lease = _lease.acquire(project_dir, publish_identity=False)
    except _lease.LeaseHeldError as e:
        return StartResult(
            run_mode="noop",
            run_id=None,
            log_file=None,
            exit_code=0,
            message=f"Launch refused: {e}",
        )

    # F-1 (REVIEW A-02): everything after acquire runs under ONE try/finally
    # — any error in the window (reconcile, liveness, closure resolution,
    # spawn) releases the lease, so a failed call can never strand a
    # live-pid lease and block the project until the caller process dies.
    # release() is idempotent and ownership-checked — one unconditional
    # release in finally covers every path (success, refusal, error).
    try:
        # DF6-2: reconcile state before continuing
        _reconcile(project_dir)
        # M2 (REVIEW виток 10): reconcile may drop the ACK this call just
        # wrote (it dedupes an ACK against an APPROVE it keeps). The answer
        # below reports the state ON DISK — re-arm the requested signal so
        # «ACK is armed» stays a fact, never a leftover claim.
        if ack and not pin_overridden:
            _arm_ack_file(paths.inbox(project_dir), ack)
        # AUD04-07: shared liveness resolver (PID file + state), same as start.
        live_pid = _liveness.resolve(project_dir)[1]
        if live_pid:
            # M8 (UX-щель 0230-а): a written ACK (the one this call just
            # armed, or a pre-existing one waiting for consumption) must not
            # be silently stranded by the noop answer — the supervisor
            # reads «running» again and never applies the decision. The
            # answer names the arming and the exact recipe.
            # A1 (REVIEW виток 2): when an explicit ``todo_id`` pin won over
            # the ack, the ack was NOT written — the armed wording would
            # lie. F4 (REVIEW виток 3): the answer names the pin's own
            # situation, not "nothing is armed". Architect-review M8: the
            # «no ACK for X» half is asserted only when the inbox agrees —
            # a consumable ACK already waiting for the pinned unit is
            # NAMED instead of denied (the old text claimed a fact the code
            # never checked).
            inbox = paths.inbox(project_dir)
            outbox = paths.outbox(project_dir)
            # I3 (REVIEW виток 6): with a pin the hint reports the PIN's own
            # ACK state — a consumable ACK waiting for ANOTHER unit must not
            # be advertised, or the recipe sends the supervisor to kill a
            # live pipeline for an answer the pinned unit cannot consume.
            pin_ack = (
                _armed_ack_file(inbox, explicit_pin)
                if explicit_pin
                and _signal_is_consumable(inbox, outbox, explicit_pin)
                else None
            )
            if pin_overridden:
                if pin_ack is not None:
                    hint = (
                        f" ACK not applied (todo_id pin wins); "
                        f"{pin_ack.name} is already waiting — kill "
                        f"the pipeline, then run awf continue with the pin "
                        f'and its ack: continue(todo_id="{explicit_pin}", '
                        f'ack="{explicit_pin}") to apply it.'
                    )
                else:
                    hint = (
                        f" ACK not applied (todo_id pin wins); no ACK for "
                        f"{explicit_pin} is waiting — kill the pipeline, then "
                        "run awf continue with the ack (no pin) to apply it."
                    )
            else:
                armed = (ack or "").strip()
                # M2 (REVIEW виток 10): «armed» is a fact on disk, checked
                # AFTER reconcile — the old text reported the requested id
                # even when reconcile had just removed its signal.
                if armed and _armed_ack_file(inbox, armed) is None:
                    armed = ""
                if not armed:
                    if explicit_pin:
                        armed = explicit_pin if pin_ack is not None else ""
                    else:
                        armed = _newest_armed_ack(project_dir)
                if armed:
                    # L1 (REVIEW виток 9): the recipe must PIN the ACK it
                    # names. A bare ``continue`` resolves the explicit
                    # todo_id pin, then the state unit, BEFORE the pending
                    # ACK — the named answer would stay stranded in the
                    # inbox, exactly the UX gap 4a closes.
                    # M2: name the REAL file when it carries the legacy short
                    # spelling (``ACK-0001.ready``), so the supervisor can
                    # recognise the signal the consumer will eat.
                    armed_file = _armed_ack_file(inbox, armed)
                    where = ""
                    if (
                        armed_file is not None
                        and armed_file.name != f"ACK-{armed}.ready"
                    ):
                        where = f" ({armed_file.name})"
                    hint = (
                        f" ACK is armed for {armed}{where}; to apply it, kill the pipeline "
                        f"(PID {live_pid}), then run awf_continue("
                        f'todo_id="{armed}", ack="{armed}").'
                    )
                elif explicit_pin:
                    # The pin's own state is empty: the recipe drops the pin
                    # instead of naming an answer the pinned unit refuses.
                    hint = (
                        f" No ACK is waiting for {explicit_pin}; drop the "
                        "todo_id pin and run awf continue without the pin to "
                        "apply an armed ACK."
                    )
                else:
                    hint = ""
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=(
                    f"Pipeline already running (PID {live_pid}). "
                    f"Use awf_status to check progress. If stuck, kill PID {live_pid} first."
                    + hint
                ),
            )

        # F-ID (REVIEW-фикс 12): passed the liveness gate — publish the
        # identity marker now, before start_in_background reads it.
        if lease is not None:
            lease.publish_identity()

        # KAUD-8: Prefer todo_id from state file (accurate crash recovery)
        # over newest_active() heuristic (might pick wrong TODO if multiple active).
        # An explicit pin (retry_stage) wins over both — the salvage unit is
        # exactly the unit to restart, whatever "newest active" says.
        # RUN8 #1: an ack pins its OWN unit (the answer belongs to exactly that
        # TODO) — priority: explicit todo_id > ack > state > newest_active.
        # The gate above resolves through the SAME helper (one priority list).
        state = read_state(project_dir)
        current_todo = _resume_target(project_dir, todo_id or "", ack or "")
        resolution_note = ""

        # Day-2 review follow-up: a REVIEW left by the supervisor (the process died
        # before anyone consumed it) must be visible in the answer, not a silent stop.
        review_todo = _pending_review_todo(project_dir)
        review_hint = ""
        if review_todo:
            review_hint = (
                f" REVIEW for {review_todo} is waiting in the outbox — refine the TODO, "
                f"then resume with `awf continue --ack {review_todo}` "
                f"(or refresh {review_todo}.ready for a replan)."
            )

        def _blocked_result(todo: str) -> StartResult:
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message=(
                    f"TODO {todo} is BLOCKED — no supervisor answer yet. "
                    f"Accept it with `awf continue --ack {todo}` (or write "
                    f".agentic/inbox/ACK-{todo}.ready); replan by creating or "
                    f"refreshing TODO-*.ready, then run awf continue again."
                    + review_hint
                ),
            )

        # Day-2 B3: a pending supervisor answer (ACK/APPROVE) must resume the TODO
        # it belongs to — even when closure signals hide it from newest_active().
        # Consume the answer, clear a BLOCKED closure, then resume.
        if current_todo:
            if _todo_closed(project_dir, current_todo):
                resolved, note = _resolve_pending_closure(project_dir, todo_id=current_todo)
                if resolved:
                    resolution_note = note
                else:
                    blocked_todo = _newest_blocked_todo(project_dir)
                    if blocked_todo:
                        return _blocked_result(blocked_todo)
                    return StartResult(
                        run_mode="noop",
                        run_id=None,
                        log_file=None,
                        exit_code=0,
                        message=f"TODO {current_todo} is closed — nothing to resume." + review_hint,
                    )
            # Active TODO — resume as-is; a pending APPROVE belongs to the commit gate.
        else:
            current_todo, resolution_note = _resolve_pending_closure(project_dir)
        if not current_todo:
            blocked_todo = _newest_blocked_todo(project_dir)
            if blocked_todo:
                return _blocked_result(blocked_todo)
            return StartResult(
                run_mode="noop",
                run_id=None,
                log_file=None,
                exit_code=0,
                message="No active TODO found." + review_hint,
            )

        # DF5-2: read pipeline state to determine resume point.
        # KAUD-8: state already read above for todo_id — reuse for from_stage.
        # The same rule the budget gate used, so both see one resume point.
        from_stage = _resume_stage(from_stage, state) or None

        if background:
            # A-02: the PID file is written inside start_in_background, so
            # the shared liveness resolver now sees the child.
            pid, log_file, _pid_file = start_in_background(
                project_dir,
                pipeline=pipeline,
                from_stage=from_stage,
                auto=auto,
                timeout=timeout,
                # AUD04-01: pin the resolved TODO — resuming from verify with an
                # empty todo_id skips the ACK/APPROVE check and hangs until timeout.
                todo_id=current_todo,
                no_checkpoints=no_checkpoints,
            )
            child_alive = _verify_child_alive(pid, log_file)
            if not child_alive:
                log_tail = ""
                try:
                    log_tail = log_file.read_text(encoding="utf-8")[-500:] if log_file else ""
                except OSError:
                    pass
                return StartResult(
                    run_mode="error",
                    run_id=pid,
                    log_file=str(log_file) if log_file else None,
                    exit_code=1,
                    message=(
                        f"Pipeline started (PID {pid}) but exited immediately. "
                        f"Last log output:\n{log_tail}"
                    ),
                )
            # RUN8 #1: the answer names the unit — a silently wrong pin was the
            # incident (same rule as retry_stage after RUN7).
            msg = (
                f"awf continue running in background (PID {pid}), "
                f"continuing {current_todo}"
            )
            if from_stage:
                msg += f", resuming from stage '{from_stage}'"
            if resolution_note:
                msg += f" ({resolution_note})"
            if ack_note:
                msg += f" {ack_note}"
            msg += ". GO IDLE — wait for user."
            # RUN8 #2: same orphan warning as start_pipeline — a worker that
            # outlived a previous kill may still be writing into the resumed unit.
            orphan_warn = orphan_worker_warning(project_dir)
            if orphan_warn:
                msg = orphan_warn + " " + msg
            return StartResult(
                run_mode="background",
                run_id=pid,
                log_file=str(log_file),
                exit_code=None,
                message=msg,
            )

        from ..orchestrator import run_pipeline

        args = PipelineArgs(
            project_dir=str(project_dir),
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
            # AUD04-01: pin the resolved TODO (same reason as the background branch).
            todo_id=current_todo,
            no_checkpoints=no_checkpoints,
        )
        try:
            exit_code = run_pipeline(args)
        except Exception as e:
            return StartResult(
                run_mode="foreground",
                run_id=None,
                log_file=None,
                exit_code=1,
                message=f"Pipeline crashed: {e}\n{traceback.format_exc()}",
            )
        message = f"Continued {current_todo}, exit code {exit_code}"
        if ack_note:
            message += f" {ack_note}"
        # RUN8 #2: orphan warning — same rule as the background branch.
        orphan_warn = orphan_worker_warning(project_dir)
        if orphan_warn:
            message = orphan_warn + " " + message
        return StartResult(
            run_mode="foreground",
            run_id=None,
            log_file=None,
            exit_code=exit_code,
            message=message,
        )
    finally:
        if lease is not None:
            lease.release()


def _clear_state_keep_salvage(project_dir: Path) -> None:
    """AUD04-08: clear pipeline state but keep the salvage counter.

    The salvage retry cycle (silent exit → salvage → retry_stage → kill →
    continue) is the documented recovery path. Wiping the counter on kill
    made the dogfood-11 escalation ("do NOT retry the same scope", attempt
    >= 2) unreachable — every retry restarted at "Attempt 1". Mirrors
    _reconcile, which preserves state keys it does not own.
    """
    from ..pipeline_state import clear_state, read_state, write_state

    state = read_state(project_dir) or {}
    try:
        count = int(state.get("salvage_count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    # R6 (AC21/AC40): the (unit, stage) entry counter must survive a
    # kill+continue — the retried stage gets -a<N+1>, not -a1 again.
    attempts = state.get("stage_attempts")
    try:
        clear_state(project_dir)
    except OSError:
        # R1.5 (REVIEW A1): clear_state propagates a real failure; salvage
        # bookkeeping stays best-effort (AUD04-08 path).
        pass
    keep = {}
    if count > 0:
        keep["salvage_count"] = count
        keep["salvage_count_key"] = state.get("salvage_count_key")
    if isinstance(attempts, dict) and attempts:
        keep["stage_attempts"] = attempts
    if keep:
        write_state(project_dir, **keep)


def _collect_worker_pids(
    project_dir: Path, pipeline_pid: int
) -> tuple[list[int], list[int]]:
    """RUN8 #2: the stage's worker pids — collected BEFORE the pipeline dies.

    Two sources, merged:
    1. state ``worker_pid`` — recorded at spawn time (with ``worker_role``
       / ``worker_todo`` for the report);
    2. /proc direct children of the pipeline (Linux) — catches a worker
       whose state record is missing (state write failed, older awf).

    Every candidate must be a LIVE direct child of the pipeline at this
    moment (the ppid check is the PID-reuse defense: a recycled number
    that is not our child is never signaled). ``pid <= 1`` and the
    pipeline itself are excluded. The second tuple element: live recorded
    pids that FAILED the child check (the pipeline already died → the
    worker was reparented — the incident's exact shape, or the number was
    recycled). They are never signaled here — only tracked for the orphan
    warning. Without /proc (non-Linux) the state record is accepted on
    liveness alone (degradation, documented).
    """
    state = read_state(project_dir) or {}
    try:
        recorded = int(state.get("worker_pid"))
    except (TypeError, ValueError):
        recorded = 0
    candidates = [recorded] if recorded else []
    candidates.extend(child_pids(pipeline_pid))

    verified: list[int] = []
    unverified: list[int] = []
    for cand in candidates:
        if cand in verified or cand in unverified:
            continue
        if cand == pipeline_pid or cand <= 1:
            continue
        if not _liveness.probe_alive(cand):
            continue
        parent = ppid_of(cand)
        if parent is None or parent == pipeline_pid:
            verified.append(cand)
        else:
            unverified.append(cand)
    return verified, unverified


def _collect_late_workers(
    project_dir: Path, pipeline_pid: int, already: set[int]
) -> tuple[list[int], list[int]]:
    """RUN10 #5 (Part B): the second sweep — workers that appeared in the
    kill window.

    The first pass (``_collect_worker_pids``) ran BEFORE the pipeline was
    signaled; a worker spawned in that gap is not in it (state held the
    previous pid, the child was not born yet). This re-reads state
    ``worker_pid`` and re-scans the pipeline's /proc children NOW, after
    the pipeline is dead, dropping pids ``already`` handled.

    The verification rule differs from the first pass: the pipeline is
    dead, so a true late worker is REPARENTED (ppid = init, the nearest
    subreaper, or the pipeline still dying) — the incident's exact shape —
    and stands on the state record written at spawn. A live candidate
    whose parent is some other live foreign process is a recycled number
    unless its own identity (an opencode-shaped cmdline, the same rule as
    :func:`orphan_worker_warning`) says it is a worker — otherwise
    unverified: never signaled, only tracked for the warning.
    """
    state = read_state(project_dir) or {}
    try:
        recorded = int(state.get("worker_pid"))
    except (TypeError, ValueError):
        recorded = 0
    candidates = [recorded] if recorded else []
    candidates.extend(child_pids(pipeline_pid))

    verified: list[int] = []
    unverified: list[int] = []
    for cand in candidates:
        if cand in already or cand in verified or cand in unverified:
            continue
        if cand == pipeline_pid or cand <= 1:
            continue
        if not _liveness.probe_alive(cand):
            continue
        parent = ppid_of(cand)
        if parent is None or parent in (1, pipeline_pid):
            # No /proc (degradation), reparented to init, or the pipeline
            # still dying — the state record is the identity.
            verified.append(cand)
        elif not _liveness.probe_alive(parent):
            # Parent already gone — reparenting in flight.
            verified.append(cand)
        else:
            cmdline = _liveness.read_cmdline(cand)
            if cmdline is not None and "opencode" in cmdline:
                # A live foreign parent can be the nearest subreaper — the
                # worker's own identity stands on its own.
                verified.append(cand)
            else:
                unverified.append(cand)
    return verified, unverified


def _write_last_kill(
    project_dir: Path,
    pipeline_pid: int,
    worker_results: dict[int, str],
) -> None:
    """RUN8 #2: persist what this kill touched — input for the next
    launch's orphan warning (the state file itself is cleared by the
    kill, so a separate one). Best-effort: a failed write only loses the
    warning, never the kill."""
    try:
        state_dir = paths.agentic_dir(project_dir) / "state"
        record = {
            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pipeline_pid": pipeline_pid,
            "workers": {str(k): v for k, v in worker_results.items()},
        }
        # AUD-0282-2: the record is a ``state`` writer like every other
        # managed surface. Both refusal types are swallowed: a failed write
        # loses the orphan warning, never the kill (``ManagedPathError`` is
        # NOT an ``OSError`` subclass).
        target = state_dir / LAST_KILL_FILE
        managed_fs.managed_write_under_agentic(target, json.dumps(record))
    except (OSError, managed_fs.ManagedPathError):
        pass


def orphan_worker_warning(project_dir: Path) -> str:
    """RUN8 #2: warn at launch when a worker from a previous kill is alive.

    Reads ``.agentic/state/last-kill.json`` (written by kill_pipeline) and
    probes the recorded worker pids. A live pid whose cmdline still says
    ``opencode`` (when /proc is readable) means the previous kill left an
    orphan whose edits may land in the NEW unit. Best-effort: no file, no
    /proc, a corrupt record → no warning, never an error.
    """
    try:
        rec_file = paths.agentic_dir(project_dir) / "state" / LAST_KILL_FILE
        record = json.loads(rec_file.read_text(encoding="utf-8"))
        workers = record.get("workers") or {}
        if not isinstance(workers, dict):
            return ""
    except (OSError, ValueError):
        return ""
    live: list[int] = []
    for wpid_s in workers:
        try:
            wpid = int(wpid_s)
        except (TypeError, ValueError):
            continue
        if wpid <= 1 or not _liveness.probe_alive(wpid):
            continue
        cmdline = _liveness.read_cmdline(wpid)
        if cmdline is not None and "opencode" not in cmdline:
            # The number was recycled for a foreign process — not our orphan.
            continue
        live.append(wpid)
    if not live:
        return ""
    pids = ", ".join(str(p) for p in sorted(live))
    return (
        f"WARNING: possible orphaned worker PID {pids} from a previous kill "
        "is still alive — its edits may land in this unit. Check it "
        "(ps) and stop it manually if it is not part of this start."
    )


def kill_pipeline(
    project_dir: Path,
) -> dict:
    """SELF-2 / RUN8 #2 / RUN10 #5: kill the running pipeline AND its
    stage worker.

    Reads pipeline_pid from state, sends SIGTERM, waits 5s, SIGKILL if
    still alive, clears state. Before the pipeline is signaled the stage
    worker is collected (state ``worker_pid`` + /proc direct children,
    ppid re-check as PID-reuse defense); after the pipeline dies, the
    worker's process group is killed the same way (TERM → grace → KILL,
    pid > 1 guard). The answer names what was killed; a worker that
    survives TERM + KILL is called out by pid — no silent orphans (the
    2026-09-23 incident: a surviving worker finished the unit after the
    kill and its edits landed in the NEXT unit's commit). A
    ``last-kill.json`` record feeds the next launch's orphan warning.

    RUN10 #5 (a): the kill is REFUSED when the calling process is inside
    the pipeline it is trying to kill (own pid → ppid → … → init via
    /proc hits the pipeline or a known worker) — the 0065 dogfood: a
    stage worker called the kill and died with its own pipeline. Answer:
    killed=false, reason="ancestry", pid=target. Without /proc the check
    degrades to an "ancestry check unavailable" note — never a block.
    RUN10 #5 (b): after the pipeline dies a SECOND sweep re-reads state
    and /proc and terminates a worker spawned during the kill window
    (the incident's late spawn); it is named in the answer ("worker PID
    N spawned during kill") and in the last-kill record.

    Returns dict with killed (bool), pid, workers ({pid: status}),
    message; reason ("ancestry") on refusal.
    """
    import os
    import signal as _signal
    import time as _time

    project_dir = Path(project_dir).resolve()
    # AUD04-07: shared resolver (PID file + state) — same view as status.
    _running, pid, _source = _liveness.resolve(project_dir)

    if not pid:
        # Clear stale state if any (AUD04-08: salvage counter survives)
        _clear_state_keep_salvage(project_dir)
        return {"killed": False, "pid": None, "message": "No running pipeline found."}

    # RUN8 #2: collect the worker(s) BEFORE signaling the pipeline — once
    # the pipeline dies, children are reparented (the ppid identity check
    # no longer works) and the state file is cleared below.
    workers, unverified = _collect_worker_pids(project_dir, pid)

    # RUN10 #5 (Part A): refuse when the CALLER is inside the pipeline it
    # is trying to kill — a stage worker calling awf_kill (the 0065
    # dogfood) took down its own pipeline and itself. The supervisor is
    # never an ancestor of the pipeline, so a legitimate stop is never
    # blocked. Without /proc the check degrades to a note in the answer.
    ancestry_hit, ancestry_ok = caller_ancestry({pid, *workers, *unverified})
    if ancestry_hit is not None:
        return {
            "killed": False,
            "reason": "ancestry",
            "pid": pid,
            "message": (
                "refusing: this process is inside the pipeline it is "
                f"trying to kill (ancestor PID {ancestry_hit}). Run kill "
                "from the supervisor session."
            ),
        }

    killed = False
    signaled = False
    try:
        os.kill(pid, _signal.SIGTERM)
        signaled = True
    except (ProcessLookupError, PermissionError, OverflowError, OSError):
        # AUD04-07: the process died between the liveness check and our
        # signal (a concurrent kill won the race). We did NOT kill it —
        # claiming success would hide the real killer from the log.
        # OverflowError: a garbage pid outside the OS range (a corrupt
        # state) — the process cannot be alive either.
        signaled = False

    if signaled:
        try:
            # Wait up to 5s for graceful shutdown
            for _ in range(10):
                _time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError, OverflowError, OSError):
                    killed = True
                    break

            if not killed:
                os.kill(pid, _signal.SIGKILL)
                _time.sleep(0.5)
                killed = True
        except (OverflowError, OSError):
            pass

    # RUN8 #2: the worker lives in its own process group (start_new_session)
    # and survives a pipeline-only kill — kill it explicitly, with the same
    # TERM → grace → KILL escalation and the pid > 1 guard.
    worker_results: dict[int, str] = {}
    for wpid in workers:
        worker_results[wpid] = kill_pid_tree(wpid)
    for wpid in unverified:
        worker_results[wpid] = "unverified"

    # RUN10 #5 (Part B): the second sweep — a worker spawned in the kill
    # window (between the first collection and the pipeline's death)
    # missed the first pass. Re-read state and /proc now that the
    # pipeline is dead and terminate what is new. Not an error: a
    # routine finishing pass (the incident's late spawn).
    late_verified, late_unverified = _collect_late_workers(
        project_dir, pid, set(worker_results)
    )
    late_pids: list[int] = []
    for wpid in late_verified:
        worker_results[wpid] = kill_pid_tree(wpid)
        late_pids.append(wpid)
    for wpid in late_unverified:
        worker_results[wpid] = "unverified"
        late_pids.append(wpid)

    # Clear state (AUD04-08: salvage counter survives the kill)
    _clear_state_keep_salvage(project_dir)

    _write_last_kill(project_dir, pid, worker_results)

    if worker_results:
        bits = [f"PID {wpid} {status}" for wpid, status in sorted(worker_results.items())]
        worker_msg = " Workers: " + ", ".join(bits) + "."
    else:
        worker_msg = " Worker: not found."
    late_msg = ""
    for wpid in late_pids:
        status = worker_results.get(wpid)
        if status == "killed":
            late_msg += (
                f" Late worker: worker PID {wpid} spawned during kill — terminated."
            )
        elif status == "dead":
            late_msg += (
                f" Late worker: worker PID {wpid} spawned during kill — already gone."
            )
    warnings = ""
    for wpid, status in sorted(worker_results.items()):
        if status == "survived":
            warnings += f" WARNING: worker PID {wpid} still alive — stop it manually."
        elif status == "unverified":
            warnings += (
                f" WARNING: worker PID {wpid} from the state record is alive "
                "but not a child of the pipeline — possible orphan, check it "
                "manually."
            )
    ancestry_note = "" if ancestry_ok else " (ancestry check unavailable)"

    if not signaled:
        return {
            "killed": False,
            "pid": pid,
            "workers": {str(k): v for k, v in worker_results.items()},
            "message": (
                f"PID {pid} already exited — nothing was killed."
                f"{worker_msg}{late_msg}{warnings}{ancestry_note}"
            ),
        }
    msg = (
        f"Pipeline killed (PID {pid})." if killed else f"Failed to kill PID {pid}."
    ) + worker_msg + late_msg + warnings + ancestry_note
    return {
        "killed": killed,
        "pid": pid,
        "workers": {str(k): v for k, v in worker_results.items()},
        "message": msg,
    }


def _salvage_todo_from_inbox(project_dir: Path) -> str:
    """TODO id from the SALVAGE-``{todo_id}``.md inbox filename (fallback).

    The engine writes ``SALVAGE-{current_todo}.md`` at salvage time, so the
    filename itself carries the unit identity even when the state file is
    gone. Multiple notes (stale ones from earlier units) → newest by mtime.
    """
    inbox = paths.inbox(project_dir)
    if not inbox.is_dir():
        return ""
    notes = [p for p in inbox.glob("SALVAGE-*.md") if p.is_file()]
    if not notes:
        return ""
    newest = max(notes, key=lambda p: p.stat().st_mtime)
    return newest.name[len("SALVAGE-") : -len(".md")]


def retry_stage(
    project_dir: Path,
    *,
    pipeline: str | None = None,
    auto: bool = False,
    timeout: int | None = None,
    background: bool = True,
) -> StartResult:
    """P2: Kill pipeline (if running) and restart from the salvage stage.

    Reads ``salvage_stage`` from state. Kills pipeline if alive. Then
    continues from that stage — effectively retrying the failed agent stage.

    The restart is PINNED to the salvaged TODO (state ``todo_id``, fallback:
    the SALVAGE note filename). Without the pin, the kill wipes the state
    (AUD04-08 keeps only the salvage counter) and continue falls back to
    the "newest active" heuristic — the AUD08-02 class that in the RUN5
    incident (2026-09-22) restarted TODO-0054 from QA instead of the
    salvaged TODO-0052. If no salvage TODO can be found the old behavior
    stands, with a warning in the answer.

    Use when salvage is triggered and supervisor wants to retry instead of
    ACK (accept) or REVIEW (reject).
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    # OPS IMP1 (R1/INV8): validate the cap BEFORE the kill — a bad value
    # must not stop a live pipeline on the way to a refusal.
    timeout = stage_timeout.validate_caller(timeout)
    timeout_refusal = _stage_timeout_refusal(project_dir)
    if timeout_refusal is not None:
        return timeout_refusal

    # Read the salvage state BEFORE the kill: kill_pipeline clears the state
    # (AUD04-08 keeps only the salvage counter), and todo_id goes with it.
    state = read_state(project_dir)
    salvage_stage = state.get("salvage_stage") if state else None

    if not salvage_stage:
        raise AwfApiError(
            "No salvage_stage in state. awf_retry_stage only works when pipeline "
            "is in salvage. Use awf_continue(from_stage=...) for manual restart."
        )

    salvage_todo = str(state.get("todo_id") or "").strip()
    if not salvage_todo:
        salvage_todo = _salvage_todo_from_inbox(project_dir)

    # TODO-0235 (M3d): the salvage cleanup below unlinks INBOX files. Check
    # the surface AND every leaf BEFORE the kill, so an unsafe path refuses
    # with no effect (REVIEW N2: the leaf check used to run after the kill).
    # One call, like the B2 closure barrier: the leaf check already walks the
    # surface, and a separate call was a second spelling of the same rule.
    inbox = paths.inbox(project_dir)
    salvage_notes = list(inbox.glob("SALVAGE-*.md"))
    refuse_unsafe_surfaces(
        project_dir, "clean salvage signals", inbox, *salvage_notes
    )

    # Kill if running
    kill_pipeline(project_dir)

    # Clean salvage signals (AUD04-08: they are written to the INBOX — the
    # old outbox glob was a no-op that left every note behind)
    for p in salvage_notes:
        try:
            p.unlink()
        except OSError:
            pass

    result = continue_pipeline(
        project_dir,
        pipeline=pipeline,
        from_stage=salvage_stage,
        auto=auto,
        timeout=timeout,
        background=background,
        todo_id=salvage_todo,
    )
    # The answer must name what was restarted — a silently wrong pin was
    # the incident; the supervisor sees the unit in the reply.
    if salvage_todo:
        result.message = (
            f"retry_stage: restarting {salvage_todo} from stage "
            f"'{salvage_stage}'. {result.message}"
        )
    else:
        result.message = (
            "WARNING: no salvage todo_id in state or SALVAGE note — no pin, "
            "newest-active fallback. " + result.message
        )
    return result


__all__ = [
    "approve_commit",
    "create_baseline",
    "rollback",
    "start_pipeline",
    "continue_pipeline",
    "kill_pipeline",
    "retry_stage",
]
