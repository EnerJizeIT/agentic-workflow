"""DAUD-7: Pipeline engine — stage handlers + transition logic.

Separates "execute a stage" (side effects) from "decide what to do next"
(transition logic). This makes run_pipeline() a thin dispatch loop.

Architecture:
- execute_supervisor_stage(): plan/verify branches
- execute_agent_stage(): subprocess + signal + transition dispatch
- _handle_next / _handle_escalate / _handle_rollback: transition handlers

No circular dependency on orchestrator — all shared helpers live here.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import budget as _budget
from . import closer_stage, paths, stage_manifest, todos, verify
from . import commit_plan as _commit_plan
from . import config as cfg_mod
from . import stage_timeout as _stage_timeout
from . import suite_guard as _suite_guard
from . import unit_contract as _unit_contract
from ._errors import AwfApiError as _AwfApiError
from ._log import log as _log
from ._worker_exit import worker_log_tail
from .agent_stage import resolve_prev_handoffs as _resolve_prev_handoffs
from .agent_stage import run_agent_stage as _run_agent_stage
from .commit_gate import maybe_commit as _maybe_commit
from .infra import managed_fs
from .pipeline import Stage
from .pipeline_state import read_state
from .pipeline_state import write_state as _write_state
from .plan_progress import mark_plan_step_done as _mark_plan_step_done
from .signal_watch import HardTimeoutError as _HardTimeoutError
from .signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    find_signal_file,
    read_signal_for_todo,
    signal_type,
    wait_for_signal,
)
from .supervisor import run_supervisor_stage as _run_supervisor_stage
from .transitions import resolve_transition
from .verify_pack import verify_pack as _verify_pack_fn

# ─── shared helpers ─────────────────────────────────────────────────────


def _find_stage_index(stages: list[Stage], name: str) -> int:
    """Find stage index by name. Returns -1 if not found."""
    for i, s in enumerate(stages):
        if s.name == name:
            return i
    return -1


def _cfg_int(config: dict, dotted_key: str, default: int) -> int:
    """automation.* config value as int — ``default`` on miss or bad type."""
    val = cfg_mod.get(config, dotted_key, default)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _find_active_todo(project_dir: Path) -> str:
    """Find newest active TODO in inbox."""
    return todos.newest_active(project_dir)


def _replan_answer(sup_signal: object, current_todo: str, project_dir: Path) -> str:
    """TODO-0213 (REVIEW fix): resolve the unit a replan runs, or "".

    The answer is exactly one of two — never a THIRD unit (a fresh unmarked
    mid-run dispatch is newest active by construction; accepting it was the
    12.10 hijack the marker removed from the waits):

    1. a TODO id in the signal naming a unit OTHER than the current one —
       the supervisor's replan answer. Both wait surfaces (interactive and
       auto subprocess) accept only a replan_for-marked TODO, so the signal
       is already marker-checked;
    2. the current unit itself, when it is still active — a rollback
       re-runs the same unit at the target stage, a rejection fallback
       retries it. The blocked unit is closed and therefore never "still
       active" — an escalation without a marked answer stops.

    Returns "" when nothing is runnable (caller stops the pipeline).
    """
    if (
        isinstance(sup_signal, str)
        and sup_signal.startswith("TODO-")
        and sup_signal != current_todo
    ):
        return sup_signal
    if current_todo and current_todo in todos.list_active_todos(
        paths.inbox(project_dir), paths.outbox(project_dir)
    ):
        return current_todo
    return ""


def _unblock_todo(project_dir: Path, todo_id: str, logs_dir: Path) -> bool:
    """Day-2 B2: clear the BLOCKED closure so the TODO counts as active again.

    Moves ``BLOCKED-{todo}.{ready,md}`` (canonical + legacy short form) from
    outbox to ``.agentic/context/`` as evidence — mirrors the manual recovery
    the user had to perform before this existed.

    Returns True only when the closure was actually cleared — no
    ``BLOCKED-<id>.ready`` marker (canonical or short) left in outbox: the
    caller's note must not claim "BLOCKED cleared" after a refusal or a
    partial move (TODO-0239 A3, REVIEW-фикс F1).
    """
    from .signals import short_id

    # Canonical first, legacy short form second — one list, so the leaf
    # collection, the preflight and the closure check cannot disagree.
    candidate_ids = list(dict.fromkeys((todo_id, short_id(todo_id))))
    outbox = paths.outbox(project_dir)
    ctx = paths.context_dir(project_dir)
    root = managed_fs.agentic_root_for(outbox)
    # TODO-0235 (REVIEW B2): the same no-follow rule as the API barrier —
    # this mover reads OUTBOX and writes CONTEXT, so both surfaces and both
    # leaves of every candidate are checked before the first move. Best
    # effort: a refusal leaves the closure in place and is logged.
    #
    # TODO-0235 (REVIEW F6): collect every leaf first and check them as ONE
    # set before the first move — a refusal must leave the closure untouched,
    # not half-moved. A broken symlink counts as a leaf too (``exists()`` is
    # False for it), so the rule is ``is_symlink() or exists()``. Surfaces and
    # leaves share ONE refusal branch: the outcome is the same, said once.
    pairs: list[tuple[Path, Path]] = []
    for candidate_id in candidate_ids:
        for ext in (".ready", ".md"):
            src = outbox / f"BLOCKED-{candidate_id}{ext}"
            if not (src.is_symlink() or src.exists()):
                continue
            pairs.append((src, ctx / src.name))
    try:
        managed_fs.require_posix("unblock todo")
        for path in (outbox, ctx, *(p for pair in pairs for p in pair)):
            managed_fs.assert_no_symlink_escape(path, root=root)
        managed_fs.managed_mkdir(ctx, root=root)
    except managed_fs.ManagedPathError as e:
        _log(
            logs_dir,
            f"Day-2 B2: BLOCKED closure not moved for {todo_id} — "
            f"unsafe managed path: {e}",
        )
        return False
    moved = False
    for src, dst in pairs:
        try:
            src.replace(dst)
            moved = True
        except OSError:
            pass
    # REVIEW-фикс F1: the closure is defined by the .ready marker, not by
    # "something moved". A partial move (the .md left outbox, the .ready
    # stayed) is not cleared — say so, so the note cannot lie.
    closure_left = any(
        (outbox / f"BLOCKED-{cid}.ready").is_symlink()
        or (outbox / f"BLOCKED-{cid}.ready").exists()
        for cid in candidate_ids
    )
    if moved and not closure_left:
        _log(logs_dir, f"Day-2 B2: BLOCKED closure moved to context/ for {todo_id}")
    elif closure_left:
        _log(
            logs_dir,
            f"Day-2 B2: BLOCKED marker remains in outbox; "
            f"closure not cleared for {todo_id}",
        )
    return moved and not closure_left


def _read_baseline_sha(project_dir: Path, todo_id: str) -> str:
    """Read baseline SHA from .agentic/context/BASELINE-{todo_id}.sha."""
    if not todo_id:
        return ""
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not sha_file.is_file():
        return ""
    return sha_file.read_text(encoding="utf-8").strip().split("\n")[0]


def _write_verify_pack_error_note(
    project_dir: Path, todo_id: str, error: Exception,
) -> None:
    """U5: best-effort GATES report when verify-pack itself crashed.

    The stage keeps going; the report carries the failure note so the
    supervisor sees WHY the mechanical checks are missing.
    """
    try:
        report = paths.context_dir(project_dir) / f"GATES-{todo_id}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            f"# GATES-{todo_id} — verify-pack report\n\n"
            f"## Verdict: **error** (verify-pack did not run)\n\n"
            f"verify-pack failed before measuring anything:\n\n"
            f"```\n{type(error).__name__}: {error}\n```\n\n"
            f"The verify stage continues; run `awf verify-pack --todo {todo_id}` "
            "manually to reproduce.\n",
            encoding="utf-8",
        )
    except OSError:
        pass  # the note is best-effort; the log line is the durable record


def _maybe_run_verify_pack(
    current_todo: str,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
) -> None:
    """U5: run the verify pack BEFORE the supervisor's signal wait.

    By the time the supervisor wakes (interactive wait or the verify
    subprocess) the GATES report is already written. Toggle:
    ``automation.verify_pack`` (default true). Skipped when there is no
    baseline (nothing to measure the diff against). Any pack error is
    logged and never stops the pipeline — an error note is written to the
    report instead.
    """
    if cfg_mod.get(config, "automation.verify_pack", True) is False:
        return
    if not _read_baseline_sha(project_dir, current_todo):
        return
    try:
        result = _verify_pack_fn(project_dir, current_todo)
    except Exception as e:  # noqa: BLE001 — the pack must never stop the stage
        _log(logs_dir, f"U5: verify-pack crashed for {current_todo}: {type(e).__name__}: {e}")
        _write_verify_pack_error_note(project_dir, current_todo, e)
        print(
            f"U5: verify-pack failed for {current_todo} ({type(e).__name__}: {e}) "
            "— verify stage continues",
            file=sys.stderr,
        )
        return
    _log(
        logs_dir,
        f"U5: verify-pack {current_todo}: {result.verdict} (exit {result.exit_code}) "
        f"→ {result.report_path}",
    )
    print(
        f"U5: verify-pack {current_todo} → {result.verdict} "
        f"(exit {result.exit_code}); report: {result.report_path}"
    )


def _ensure_baseline_sha(
    project_dir: Path, todo_id: str, logs_dir: Path,
) -> None:
    """П3: auto-create baseline SHA file if missing.

    Was: supervisor had to run `awf baseline TODO-NNNN` manually after
    creating TODO.md (boilerplate). Now orchestrator ensures baseline
    exists right before agent stage starts. If user/supervisor already
    created one via `awf baseline` (richer status, tests log, etc.) —
    we leave it alone.
    """
    from . import git_utils
    from ._atomic import atomic_write_text

    if not todo_id:
        return
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if sha_file.exists():
        return  # already created by `awf baseline` or previous run

    if not git_utils.is_git_repo(project_dir):
        _log(logs_dir, f"П3: skip baseline for {todo_id} — not a git repo")
        return

    try:
        sha = git_utils.current_sha(project_dir)
        atomic_write_text(sha_file, sha + "\n")
        _log(logs_dir, f"П3: auto-created baseline {sha[:8]} for {todo_id}")
    except Exception as e:
        _log(logs_dir, f"П3: baseline creation failed for {todo_id}: {e}")


# ─── transition handlers ────────────────────────────────────────────────


def _consume_verify_decision(
    project_dir: Path,
    current_todo: str,
    signal: str,
    logs_dir: Path,
) -> None:
    """AUD04-04: consume the verify decision signal once the cycle acted on it.

    Analog of the DONE consumption in execute_agent_stage (NEG-4): a decision
    file must not survive the cycle that accepted it. Without this, a
    commit-fail or a kill left the ACK/APPROVE/REVIEW in place, and a
    re-verify of the same TODO (``awf continue``) re-accepted the old decision
    — e.g. auto-committing on a dead approval. The mtime gate in
    wait_for_supervisor_signal is the safety net for files a dead process
    never got to consume.
    """
    if not signal:
        return
    if signal.startswith("REVIEW-"):
        candidates = [paths.outbox(project_dir) / f"REVIEW-{current_todo}.md"]
    elif signal.startswith(("ACK-", "APPROVE-")):
        prefix = signal.split("-", 1)[0]
        candidates = [paths.inbox(project_dir) / f"{prefix}-{current_todo}.ready"]
    else:
        return
    managed_fs.require_posix("consume verify decision")
    # TODO-0235 (M3d): a symlinked OUTBOX (REVIEW) or INBOX (ACK/
    # APPROVE) used to send this unlink out of the project. Fail
    # closed — the run is untrustworthy anyway. The branch picks the
    # surface; the root comes from each leaf, so both are covered without
    # a union list. All leaves are checked BEFORE the first unlink: a
    # refusal must not consume half the set (REVIEW F6's rule).
    for p in candidates:
        managed_fs.assert_no_symlink_escape(
            p, root=managed_fs.agentic_root_for(p)
        )
    for p in candidates:
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        else:
            _log(logs_dir, f"AUD04-04: consumed verify decision {p.name}")
    # U6b: the decision has been acted on — drop the acceptance record so the
    # next cycle starts fresh (a fresh re-approval must not be mistaken for
    # this cycle's leftover).
    _write_state(
        project_dir,
        accepted_decision=None, accepted_decision_mtime=None,
        logs_dir=logs_dir,
    )


def _consume_verify_decision_guarded(
    project_dir: Path,
    current_todo: str,
    signal: str,
    logs_dir: Path,
    stage_name: str,
) -> bool:
    """TODO-0235 (REVIEW N1): consume the verify decision without a raw escape.

    The barrier inside ``_consume_verify_decision`` raises on a symlinked
    outbox/inbox; left uncaught it reached the CLI's last-resort handler with
    no stage record. False means the refusal was recorded and the caller
    should stop the pipeline.

    B1 (REVIEW виток 3): ``stage_name`` is the caller's actual stage name
    (``s_name``), not the literal "verify" — the last stage of a custom
    pipeline may have any name, and ``retry_stage`` looks the salvage stage up
    by that name in the pipeline.
    """
    try:
        _consume_verify_decision(project_dir, current_todo, signal, logs_dir)
    except managed_fs.ManagedPathError as e:
        _unsafe_stage_stop(project_dir, current_todo, stage_name, logs_dir, e)
        return False
    return True


def _record_verify_decision(
    project_dir: Path,
    current_todo: str,
    signal: str,
    logs_dir: Path,
) -> None:
    """U6b: record the accepted verify decision BEFORE the cycle acts on it.

    A kill between acceptance and consumption (AUD04-04) leaves the decision
    file on disk. This record — signal name + the file's mtime at acceptance,
    keyed per signal in the pipeline state — is what makes the next verify's
    fallback (supervisor._detect_supervisor_signal) recognize the leftover as
    the SAME decision instead of a fresh approval.
    """
    if not signal:
        return
    if signal.startswith("REVIEW-"):
        candidates = [paths.outbox(project_dir) / f"REVIEW-{current_todo}.md"]
    elif signal.startswith(("ACK-", "APPROVE-")):
        prefix = signal.split("-", 1)[0]
        candidates = [paths.inbox(project_dir) / f"{prefix}-{current_todo}.ready"]
    else:
        return
    for p in candidates:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        _write_state(
            project_dir,
            accepted_decision=signal,
            accepted_decision_mtime=mtime,
            logs_dir=logs_dir,
        )
        _log(logs_dir, f"U6b: recorded accepted verify decision {p.name} (mtime={mtime})")
        return


def _commit_outcome_ok(
    outcome: _commit_plan.CommitOutcome,
    s_name: str,
    current_todo: str,
    logs_dir: Path,
) -> bool:
    """R-03: one handling of the typed gate result for BOTH stages.

    committed/skipped → the cycle may continue; refused/error → it stops
    (exit code 1, the TODO stays active, changes remain in the working
    tree). Before R-03 the execute stage ignored the gate's boolean
    refusal (A-14) and advanced anyway.
    """
    if outcome.status in _commit_plan.PROCEED_STATUSES:
        return True
    print(
        f"Commit gate {outcome.status} for {current_todo} at '{s_name}': "
        f"{outcome.reason}",
        file=sys.stderr,
    )
    print(
        "  TODO stays ACTIVE; changes remain in the working tree for manual review.",
        file=sys.stderr,
    )
    _log(
        logs_dir,
        f"Commit gate {outcome.status} for {current_todo} at {s_name}: {outcome.reason}",
    )
    return False


def _handle_next(
    project_dir: Path,
    logs_dir: Path,
    s_name: str,
    current_todo: str,
    action: str,
    auto: bool,
    retry_counts: list[int],
    stage_idx: int,
) -> tuple[int, int]:
    """Transition: next / commit_and_next / commit_and_report.

    Returns (new_stage_idx, exit_code). The gate's typed result (R-03) is
    handled exactly like on the verify stage: a refused/failed commit
    stops the cycle (exit_code 1, stage unchanged) instead of advancing —
    the A-14 boolean-ignore path is gone.
    """
    baseline_sha = _read_baseline_sha(project_dir, current_todo)
    outcome = _maybe_commit(
        s_name, current_todo, action, project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha
    )
    if not _commit_outcome_ok(outcome, s_name, current_todo, logs_dir):
        return stage_idx, 1
    print("Moving to next stage.")
    retry_counts[stage_idx] = 0
    return stage_idx + 1, 0


def _handle_escalate(
    project_dir: Path,
    logs_dir: Path,
    s_name: str,
    current_todo: str,
    auto: bool,
    stage: Stage,
    retry_counts: list[int],
    stage_idx: int,
    pipeline_name: str | None = None,
) -> tuple[int, str, int]:
    """Transition: BLOCKED → supervisor replan.

    The ``max_retries`` budget slot is per stage INSTANCE (its position in
    the pipeline — the stage id when declared), not per role: two stages
    of the same role never share a counter (ORCH M3.2 invariant 3).

    Two answers: ACK/APPROVE = "accept the current state" — the SAME unit
    retries the SAME stage. A replan-marked TODO = a NEW unit — it starts
    the full pipeline from stage 0 (TODO-0213, BD-30: the old behavior
    resumed it at the blocked stage, so the implementer stage was skipped
    and the unit ran with the previous unit's unreviewed work as its own).
    The new unit is the one the replan wait picked up (its TODO id in the
    signal — a replan_for-marked TODO, TODO-0213) or the current unit when
    it is still active (a rejection fallback routed here). Any other signal
    is no answer and the pipeline stops on the blocked unit (REVIEW fix —
    the pre-0213 ``_find_active_todo`` fallback for an empty signal was the
    same false positive on the auto surface: an unmarked mid-run dispatch,
    newest by construction, hijacked the pipeline; a third unit is never
    the answer, see ``_replan_answer``).

    Returns (new_stage_idx, new_current_todo, exit_code).
    exit_code != 0 means pipeline should stop.
    """
    max_r = stage.max_retries
    if retry_counts[stage_idx] >= max_r:
        print(f"BLOCKED — max retries reached ({max_r}). Pipeline stopped.")
        _log(logs_dir, f"Max retries reached for stage {s_name}")
        return stage_idx, current_todo, 1

    retry_counts[stage_idx] += 1
    print(f"BLOCKED — escalating to supervisor (attempt {retry_counts[stage_idx]}/{max_r})")
    _log(logs_dir, "Escalating to supervisor for retry")

    replan_stage = Stage(name="replan", role="supervisor", kind="replan")
    # AUD04-05: a replan timeout/crash on this path used to escape as a raw
    # traceback (dirty state, dashboard stuck on "running") — same class of
    # event as the main supervisor stage, same clean stop.
    try:
        sup_signal = _run_supervisor_stage(
            replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
        )
    except (RuntimeError, TimeoutError) as e:
        print("ERROR: supervisor replan after BLOCKED crashed. Pipeline stopped.", file=sys.stderr)
        print(f"  Details: {e}", file=sys.stderr)
        _log(logs_dir, f"Pipeline stopped at stage {s_name}: escalate replan crashed: {e}")
        return stage_idx, current_todo, 1

    # Day-2 B2: ACK/APPROVE from the supervisor means "accept the current
    # state, keep going". Clear the BLOCKED closure (TODO becomes active
    # again) and retry the same stage with the same TODO.
    if isinstance(sup_signal, str) and sup_signal.startswith(("ACK-", "APPROVE-")):
        _unblock_todo(project_dir, current_todo, logs_dir)
        print(f"Supervisor accepted the blocked state — retrying stage '{s_name}'")
        return stage_idx, current_todo, 0

    # TODO-0213 (BD-30): the replan wait picked up a SPECIFIC unit (the
    # marked TODO's id is the signal) — run exactly that one. The signal
    # is the blocked unit itself when the auto fallback detection returns
    # the stale TODO-*.ready of the blocked unit — a replan REPLACES the
    # unit, it never is it, so that is not an answer (the interactive wait
    # already filters it out at the marker gate).
    #
    # TODO-0213 (REVIEW fix): the pre-fix "_find_active_todo" fallback for
    # an empty signal was the same false positive on the auto surface — a
    # fresh unmarked mid-run dispatch (newest active by construction)
    # hijacked the pipeline. The answer is the marked signal or the
    # current unit when still active (a rejection fallback); a blocked
    # unit is closed, so an escalation without a marked answer stops
    # cleanly (the BLOCKED closure stays intact; the owner dispatches a
    # replan_for-marked TODO and continues).
    new_todo = _replan_answer(sup_signal, current_todo, project_dir)
    if not new_todo:
        print(
            "Supervisor did not answer the escalation (no replan_for-marked "
            "TODO). Stopping.",
        )
        _log(logs_dir, "Escalate: no replan answer — stopping on the blocked unit")
        return stage_idx, current_todo, 1
    print(f"New TODO: {new_todo} — restarting the pipeline from the first stage")
    _log(
        logs_dir,
        f"BD-30: replanned unit {new_todo} starts at stage 0 "
        f"(the full pipeline, not the blocked stage {s_name})",
    )
    return 0, new_todo, 0  # TODO-0213: the replan unit runs the full pipeline


def _rollback_budget_allow(
    project_dir: Path,
    logs_dir: Path,
    stages: list[Stage],
    current_todo: str,
    target: str,
    target_idx: int,
    source: str,
) -> tuple[bool, int, int]:
    """AUD03-01: count one rollback and say whether it may happen.

    The ONE budget rule for both rollback flavors (supervisor replan and
    the spec FAIL's direct rewind): per (TODO, source->target), limit is
    the target stage's ``max_rollbacks``. Returns (allowed, count, limit);
    the count is the number of this rollback (1-based) for the message.
    """
    prev_state = read_state(project_dir) or {}
    counts = prev_state.get("rollback_counts")
    if not isinstance(counts, dict):
        counts = {}
    rb_key = f"{current_todo}:{source}->{target}"
    try:
        rb_count = int(counts.get(rb_key, 0) or 0)
    except (TypeError, ValueError):
        rb_count = 0
    limit = stages[target_idx].max_rollbacks
    if rb_count >= limit:
        print(
            f"Rollback budget exhausted ({limit}) for {current_todo} "
            f"({source or '?'} -> {target}). Escalating to supervisor. "
            f"Pipeline stopped.",
            file=sys.stderr,
        )
        _log(logs_dir, f"Rollback budget exhausted for {rb_key} — pipeline stopped")
        return False, rb_count, limit

    counts[rb_key] = rb_count + 1
    _write_state(project_dir, rollback_counts=counts, logs_dir=logs_dir)
    return True, rb_count + 1, limit


def _handle_direct_rollback(
    project_dir: Path,
    logs_dir: Path,
    stages: list[Stage],
    current_todo: str,
    target: str,
    source: str = "",
) -> tuple[int, str, int]:
    """SPEC: rewind to the author stage WITHOUT a supervisor replan.

    A rejecting verdict in the declared output is itself the decision — a
    requirements review ``Статус: FAIL`` or a readiness ``Статус: BLOCKED``
    (S14): the SAME unit returns to the author stage and the review runs
    again. Budgeting is the shared rollback rule
    (:func:`_rollback_budget_allow`).

    Returns (new_stage_idx, new_current_todo, exit_code).
    """
    target_idx = _find_stage_index(stages, target)
    if target_idx < 0:
        print(f"ERROR: Rollback target '{target}' not found in pipeline")
        _log(logs_dir, f"SPEC: rollback target {target!r} not found")
        return -1, current_todo, 1

    allowed, count, limit = _rollback_budget_allow(
        project_dir, logs_dir, stages, current_todo, target, target_idx, source
    )
    if not allowed:
        return -1, current_todo, 1

    print(
        f"SPEC: declared verdict rejects the stage — rolling back to: "
        f"{stages[target_idx].name} (rollback {count}/{limit})"
    )
    _log(
        logs_dir,
        f"SPEC: automatic rollback to stage {stages[target_idx].name} "
        f"(index {target_idx}, {count}/{limit}, no supervisor replan)",
    )
    return target_idx, current_todo, 0


def _handle_rollback(
    project_dir: Path,
    logs_dir: Path,
    stages: list[Stage],
    current_todo: str,
    auto: bool,
    target: str,
    pipeline_name: str | None = None,
    source: str = "",
) -> tuple[int, str, int]:
    """Transition: rollback to a target stage + supervisor replan.

    AUD03-01: rollbacks are budgeted per (TODO, source->target) in the
    pipeline state — a systematic reject/failed signal used to loop
    rollback->replan->stage forever (and self-rollback looped in place).
    Exhaustion stops the pipeline so the supervisor decides.

    Returns (new_stage_idx, new_current_todo, exit_code).
    """
    target_idx = _find_stage_index(stages, target)
    if target_idx < 0:
        print(f"ERROR: Rollback target '{target}' not found in pipeline")
        return -1, current_todo, 1

    allowed, count, limit = _rollback_budget_allow(
        project_dir, logs_dir, stages, current_todo, target, target_idx, source
    )
    if not allowed:
        return -1, current_todo, 1

    print(f"Rolling back to stage: {stages[target_idx].name} (rollback {count}/{limit})")
    _log(
        logs_dir,
        f"Rollback to stage {stages[target_idx].name} (index {target_idx}, {count}/{limit})",
    )

    replan_stage = Stage(name="replan", role="supervisor", kind="replan")
    # AUD04-05: same clean stop as the main supervisor stage — a replan
    # timeout/crash must not escape as a raw traceback.
    try:
        sup_signal = _run_supervisor_stage(
            replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
        )
    except (RuntimeError, TimeoutError) as e:
        print("ERROR: supervisor replan after rollback crashed. Pipeline stopped.", file=sys.stderr)
        print(f"  Details: {e}", file=sys.stderr)
        _log(logs_dir, f"Pipeline stopped at stage {source or target}: rollback replan crashed: {e}")
        return -1, current_todo, 1

    # TODO-0213 (REVIEW fix): the replan answer is the marked TODO's id in
    # the signal (a TODO id naming a unit OTHER than the current one) or
    # the SAME unit, still active — the pre-fix "_find_active_todo"
    # fallback could return a fresh unmarked mid-run dispatch instead
    # (newest active by construction): the same hijack the marker removed
    # from the waits. A third unit is never the rollback's answer; nothing
    # runnable (M1) stops the pipeline, never an empty todo.
    new_todo = _replan_answer(sup_signal, current_todo, project_dir)
    if not new_todo:
        print("Rollback: no runnable unit after replan. Stopping.", file=sys.stderr)
        _log(logs_dir, "Rollback: no new TODO after replan — stopping")
        return -1, current_todo, 1
    return target_idx, new_todo, 0


# ─── checkpoint + salvage ───────────────────────────────────────────────


def _run_plan_checkpoint_gate(
    current_todo: str,
    project_dir: Path,
    config: dict,
    auto: bool,
    logs_dir: Path,
) -> int:
    """BD-36: Plan checkpoint dispatch.

    Runs after supervisor's plan stage. If checkpoint is enabled, opens
    the HTML form and waits for user decision. Returns:

      - ``0`` — checkpoint passed (approve / edit / timeout / disabled),
                 pipeline should continue.
      - ``1`` — checkpoint rejected (or other failure), pipeline must stop.
    """
    from . import run_state as _run_state
    from .plan_checkpoint import (
        is_checkpoint_enabled,
        launch_no_checkpoints,
        run_plan_checkpoint,
    )

    # The effective skip flag is computed ONCE here — the single place that
    # combines the two launch-scoped sources (config/env/auto stay inside
    # is_checkpoint_enabled):
    #   RUN3 #6: the no_checkpoints start/continue parameter — env for the
    #   duration of THIS pipeline process, dies with it.
    launch_flag = launch_no_checkpoints()
    # B4: an ACTIVE run started with no_checkpoints=true does not expect the
    # owner at every TODO. The flag is read from the run state (a finished
    # run must not skip checkpoints of a later manual start).
    run = _run_state.read_run(project_dir)
    run_flag = bool(run and run.get("active") and run.get("no_checkpoints"))
    no_checkpoints = launch_flag or run_flag
    if not is_checkpoint_enabled(config, auto, no_checkpoints=no_checkpoints):
        if launch_flag:
            print(
                "BD-36: checkpoint skipped — start passed no_checkpoints=true "
                "(single launch)"
            )
            _log(
                logs_dir,
                f"RUN3-6: checkpoint skipped for {current_todo} — "
                "start no_checkpoints=true",
            )
        elif run_flag:
            print("BD-36: checkpoint skipped — run started with no_checkpoints=true (B4)")
            _log(
                logs_dir,
                f"B4: checkpoint skipped for {current_todo} — run no_checkpoints=true",
            )
        return 0

    decision = run_plan_checkpoint(current_todo, project_dir, config, logs_dir)

    if decision == "reject":
        print(
            f"BD-36: Plan checkpoint rejected for {current_todo}. "
            f"Pipeline stopped — supervisor will replan on next 'awf start'.",
            file=sys.stderr,
        )
        _log(logs_dir, f"BD-36: checkpoint rejected for {current_todo}")
        return 1

    if decision == "timeout":
        print(
            "BD-36: Plan checkpoint timed out — NO auto-approve. "
            "Pipeline stopped. Re-run 'awf start' when ready to review.",
            file=sys.stderr,
        )
        _log(
            logs_dir,
            f"BD-36: checkpoint timed out for {current_todo} — pipeline aborted "
            "(user must re-run awf_start after manual review)",
        )
        return 1

    # "approve" or "edit" → continue normally
    _log(logs_dir, f"BD-36: checkpoint decision={decision}")
    return 0


def _write_salvage_note(note: Path, body: str, logs_dir: Path) -> bool:
    """The ONE writer of ``SALVAGE-{todo}.md`` — under the managed barrier.

    M3d (REVIEW architect-review): two call sites wrote the same file with a
    bare ``atomic_write_text`` — the silent-exit prompt and the unsafe-path
    stop. A write next to a second implementation is how the F-A hole came
    back: ``atomic_write_text`` renames through the path, so a symlinked
    ``inbox`` sent the note outside the project. The barrier runs from the
    leaf (one root convention, see ``managed_write_text``); refusal is
    logged, not raised — the note is best-effort, the stage record and the
    salvage state already exist.
    """
    try:
        managed_fs.managed_write_text(
            note, body, root=managed_fs.agentic_root_for(note)
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(logs_dir, f"salvage note not written ({note.name}): {e}")
        return False
    return True


def _write_salvage_prompt(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    baseline_sha: str | None,
    logs_dir: Path,
    attempt: int = 1,
    auto_retries: int = 0,
) -> None:
    """DF5-4: Write a SALVAGE-{todo_id}.md file to inbox.

    This file explains to the supervisor (in opencode) what happened:
    - Worker ran but didn't produce a DONE/BLOCKED signal
    - Git diff stat shows what work was left
    - Supervisor needs to decide: ACK (accept), REVIEW (reject), or replan
    - Dogfood-11: escalates when the stage is not converging — repeat
      salvages OR an exhausted automatic-retry budget (``auto_retries``) —
      telling the supervisor not to retry the same scope, but to split the
      task / require incremental writes / change the stage model instead.
    """
    inbox = paths.inbox(project_dir)
    salvage_file = inbox / f"SALVAGE-{todo_id}.md"

    parts: list[str] = [
        f"# Salvage needed: {stage_name} did not signal",
        "",
        f"**TODO:** {todo_id}",
        f"**Stage:** {stage_name}",
        f"**Attempt:** {attempt} (consecutive silent exits at this stage)",
        f"**Time:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## What happened",
        "",
        f"The worker at stage `{stage_name}` completed its subprocess (exit 0) but",
        f"did NOT create the DONE-{todo_id}.ready signal file in outbox.",
        "Two common causes: (1) the worker finished but forgot the signal —",
        "then the git diff below shows work; (2) the worker hit its output-token",
        "limit mid-reply — the reply was truncated, no tool call was made, and",
        "the process exited cleanly. Empty diff + no progress notes = likely (2).",
        "",
    ]

    if auto_retries:
        parts += [
            f"Automatic continue-pushes after the silent exits: {auto_retries} "
            f"(all ended without a signal).",
            "",
        ]

    if attempt >= 2 or auto_retries >= 2:
        header = (
            f"## ⚠️ ATTEMPT {attempt}: do NOT retry the same scope"
            if attempt >= 2
            else "## ⚠️ do NOT retry the same scope"
        )
        parts += [
            header,
            "",
            f"Evidence: salvage rounds in a row: {attempt}; automatic "
            f"continue-pushes: {auto_retries} — nothing converged.",
            "Restarting the same TODO as-is will very likely fail the same way.",
            "Change ONE of these before retrying:",
            "",
            "1. **Split the task** — one TODO per file (or per function/section).",
            "   Small scoped tasks are the universal fix for models with a limited",
            "   output budget.",
            "2. **Require incremental writes** in the TODO — \"create file X, then",
            "   add function Y\", so the worker never needs one huge reply.",
            "3. **Change the stage model** — pick a model without a tight output",
            "   limit, or reduce its reasoning/verbosity if configurable.",
            "",
        ]

    # Git diff stat
    if baseline_sha:
        try:
            diff = subprocess.run(
                ["git", "diff", "--stat", baseline_sha],
                cwd=str(project_dir),
                capture_output=True, text=True, errors="replace",
                check=False, timeout=10,
            )
            diff_output = diff.stdout.strip() if diff.stdout else "(no changes)"
        except (subprocess.TimeoutExpired, OSError):
            diff_output = "(git diff failed)"
        parts += [
            "## Git diff (vs baseline)",
            "",
            "```",
            diff_output,
            "```",
            "",
        ]

    parts += [
        "## What to do",
        "",
        f"1. **Review the diff** — did `{stage_name}` produce useful work?",
        f"2. **Check handoffs** — read `.agentic/handoff/{stage_name}-{todo_id}.md`",
        "3. **Decide:**",
        f"   - Work looks good → create `.agentic/inbox/ACK-{todo_id}.ready`",
        f"   - Work is wrong → create `.agentic/outbox/REVIEW-{todo_id}.md` with feedback",
        "   - Need to redo → create a new TODO and restart pipeline",
        "",
    ]

    if _write_salvage_note(salvage_file, "\n".join(parts), logs_dir):
        _log(logs_dir, f"DF5-4: salvage prompt written to {salvage_file}")


def _silent_retry_note(attempt: int, todo_id: str) -> str:
    """F7 (dogfood-11): push text for a stage retried after a silent exit.

    Weak models end the opencode loop with a text answer (or get cut off by
    their output-token limit mid-reply) instead of writing code and
    signalling. This note is appended to the retried prompt: no re-research,
    act now, finish with the signal.
    """
    return (
        f"## ⚠️ RETRY {attempt}: previous attempt ended WITHOUT code and WITHOUT a signal\n"
        f"The stage restarted because the previous run produced no file changes and no\n"
        f"DONE/BLOCKED signal — that attempt FAILED. Do not re-read documents, do not\n"
        f"re-plan, do not answer with a summary. Write the required files NOW:\n"
        f"create a file skeleton first, then fill in one function or section per step.\n"
        f"Finish by creating the signal file:\n"
        f"  touch .agentic/outbox/DONE-{todo_id}.ready\n"
        f"If a reply is getting long, stop and act with tools — long text replies get\n"
        f"cut off by the output limit and count as failure. If you truly cannot proceed,\n"
        f"create the BLOCKED signal instead of ending with plain text.\n"
    )


def _progress_block(
    project_dir: Path, log_holder: dict[str, str], current_todo: str, outbox: Path,
) -> list[str]:
    """TODO-0226: where the previous run stopped — paths, not prose.

    The retried worker gets the stage's worker log and the latest DONE
    draft (when one exists) instead of re-reading the whole task from
    scratch. Paths are project-relative for a readable prompt.
    """
    def _rel(raw: str) -> str:
        p = Path(raw)
        try:
            return str(p.relative_to(project_dir))
        except ValueError:
            return raw

    lines = ["## Progress so far", ""]
    log_path = log_holder.get("log_path", "")
    if log_path:
        lines.append(f"Last worker log: `{_rel(log_path)}`")
    draft = find_signal_file(outbox, "DONE", current_todo, ".md")
    if draft is not None:
        lines.append(f"Last DONE draft: `{_rel(str(draft))}`")
    lines.append(
        "Continue from where you stopped — do NOT re-read everything from "
        "scratch."
    )
    return lines


def _silent_work_retry_note(
    attempt: int, todo_id: str, *,
    progress: list[str] | None = None,
) -> str:
    """TODO-0196: push text for a stage retried after a silent exit that
    LEFT WORK in the tree. The previous run changed files but ended without
    the signal — the retry continues the work instead of redoing it.

    TODO-0226: the 2nd retry also reduces the pass (no full suite /
    run-all — targeted checks only) and every retry names the partial
    artifacts (worker log + DONE draft) so the worker resumes instead of
    re-researching.
    """
    parts = [
        f"## ⚠️ RETRY {attempt}: previous attempt left WORK in the tree but NO signal",
        "The stage restarted because the previous run wrote file changes but did",
        f"not create DONE-{todo_id}.ready — the work exists, only the finish is missing.",
        "Do NOT redo or rewrite what is already in the tree: check git status / git",
        "diff, continue from where the previous run stopped, and finish the work.",
        "Finish by creating the signal file:",
        f"  touch .agentic/outbox/DONE-{todo_id}.ready",
        "If you truly cannot proceed, create the BLOCKED signal instead of ending",
        "with plain text.",
    ]
    if attempt >= 2:
        parts += [
            "",
            "## ⚠️ RETRY 2: reduce this pass",
            "Do NOT run the full suite or scripts/run-all.sh in this pass — run only",
            "the targeted checks and the gates, then write the DONE report.",
        ]
    if progress:
        parts += ["", *progress]
    return "\n".join(parts) + "\n"


def _declared_output_problem(
    project_dir: Path, output: str, stage_start_wall: float,
) -> str:
    """ORCH M3.2: why the stage's declared output does not count — or "".

    The declared output (project-relative) must exist AND be fresh: its
    mtime no older than the stage entry's start, whole-second resolution
    (the AUD04-04 convention — filesystem mtime granularity can be 1s, so
    a file written in the stage's start second is fresh). A leftover from
    an earlier stage or run predates the start and does not count as this
    stage's work.
    """
    p = project_dir / output
    if not p.is_file():
        return f"declared output '{output}' does not exist"
    try:
        mtime = int(p.stat().st_mtime)
    except OSError:
        return f"declared output '{output}' is not readable"
    if mtime < int(stage_start_wall):
        return (
            f"declared output '{output}' is stale "
            f"(last modified before this stage started)"
        )
    return ""


def _verdict_document(project_dir: Path, output: str) -> tuple[str, str] | None:
    """The role and text of a verdict artifact, or ``None``.

    One reader for the review and the readiness document: both are
    identified by file name and decoded the same way, so the verdict and
    the rewind decision cannot drift on the role table or the decode
    policy. A foreign document, a missing file or a non-UTF-8 file is
    ``None`` — the stage keeps its own signal.
    """
    from . import spec_package as _spec

    path = project_dir / output
    role = _spec.role_for_name(path.name)
    if role not in (_spec.REVIEW_ROLE, _spec.READINESS_ROLE):
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return role, text


def _declared_review_verdict(project_dir: Path, output: str) -> str:
    """SPEC: the signal a verdict artifact's ``Статус`` implies — or "".

    Two documents are verdicts, and both are the stage's own decision:

    - the requirements review: ``Статус: FAIL`` rejects the stage, ``PASS``
      advances it;
    - the readiness report (S14): ``Статус: BLOCKED`` rejects it exactly
      like a review FAIL — the package goes back to its author without a
      supervisor replan, ``READY`` advances it.

    Whatever signal the worker left, the document wins. Any other package
    document (a ``Статус`` quoted in requirements, architecture or the test
    spec) carries no verdict; a read problem leaves the stage's own signal
    untouched.
    """
    from . import spec_package as _spec

    document = _verdict_document(project_dir, output)
    if document is None:
        return ""
    role, text = document
    status = _spec.declared_status(text)
    rejecting = "FAIL" if role == _spec.REVIEW_ROLE else "BLOCKED"
    return "rejected" if status == rejecting else ""


def _readiness_rollback_target(
    project_dir: Path, output: str, stages: list[Stage]
) -> str:
    """S14: the author stage a BLOCKED readiness belongs back at, or "".

    The readiness report's first open question names its owner. An
    architecture owner rewinds to the architect stage, every other owner
    (requirements, analysis, an empty section) to the analyst stage — the
    package's root author. The stage is found by ROLE, not by a hardcoded
    stage name, so a renamed or reordered pipeline still lands on the
    author. A pipeline without the role yields "" — the declared policy
    target stays in force.
    """
    from . import spec_package as _spec

    document = _verdict_document(project_dir, output)
    if document is None or document[0] != _spec.READINESS_ROLE:
        return ""
    question = _spec.first_open_question(document[1])
    owner = question[1].lower() if question else ""
    architecture = "архит" in owner or "architect" in owner
    role = "agent-architector" if architecture else "agent-system-analyst"
    for stage in stages:
        if stage.role == role:
            return stage.name
    return ""


# ─── stage execution ────────────────────────────────────────────────────


def execute_supervisor_stage(
    stage: Stage,
    current_todo: str,
    auto: bool,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
    pipeline_name: str | None = None,
) -> tuple[str, int, int]:
    """Execute one supervisor stage.

    Returns (new_current_todo, stage_idx_delta, exit_code).
    - stage_idx_delta: +1 for next stage, 0 for retry
    - exit_code: 0 = success, 1 = stop pipeline
    """
    s_name = stage.name
    s_kind = stage.kind

    # TODO-0262: a unit that used up its review-pass budget does not enter
    # verify at all — the BUDGET note goes to the owner and the pipeline
    # stops. A broken limit is a config refusal, not a silent pass-through.
    if s_kind == "verify" and current_todo:
        try:
            reason = _budget.check_and_note(
                project_dir,
                current_todo,
                config,
                logs_dir=logs_dir,
            )
        except _budget.BudgetConfigError as e:
            print(f"ERROR: invalid limits.max_review_passes: {e}", file=sys.stderr)
            _log(logs_dir, f"verify: budget config refused for {current_todo}: {e}")
            return current_todo, 0, 1
        except OSError as e:
            print(f"ERROR: cannot write the budget note: {e}", file=sys.stderr)
            _log(logs_dir, f"verify: budget note failed for {current_todo}: {e}")
            return current_todo, 0, 1
        if reason:
            print(f"STOP: {reason}. See outbox/BUDGET-{current_todo}.md.", file=sys.stderr)
            _log(logs_dir, f"verify: {reason} — pipeline stopped")
            return current_todo, 0, 1

    # U5: the verify pack must finish BEFORE the supervisor waits for its
    # signal — the GATES report is ready when the supervisor wakes.
    if s_kind == "verify" and current_todo:
        _maybe_run_verify_pack(current_todo, project_dir, config, logs_dir)

    try:
        sup_signal = _run_supervisor_stage(
            stage, current_todo, auto, project_dir, logs_dir, pipeline_name
        )
    except (RuntimeError, TimeoutError) as e:
        print(f"ERROR: supervisor stage '{s_name}' crashed. Pipeline stopped.", file=sys.stderr)
        print(f"  Details: {e}", file=sys.stderr)
        _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
        return current_todo, 0, 1

    if s_kind == "plan":
        # FU-05: single-phase plan — the contract is the TODO itself. The
        # two-phase brief flow is gone: stray brief-signal leftovers in the
        # inbox no longer influence TODO selection (AUD04-03).
        # NEG-2026-09-19 R1: keep the pinned TODO if the caller supplied one
        # (run queue); fall back to "newest active" only when unpinned.
        current_todo = current_todo or _find_active_todo(project_dir)
        if not current_todo:
            print("No active TODO found. Create one first, then continue.")
            _log(logs_dir, "No active TODO after supervisor stage")
            return current_todo, 0, 1

        rc = _run_plan_checkpoint_gate(current_todo, project_dir, config, auto, logs_dir)
        if rc != 0:
            return current_todo, 0, rc

        print(f"Active TODO: {current_todo}")
        _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir, phase="brief")
        return current_todo, 1, 0  # next stage

    if s_kind == "verify":
        # AUD02-01: persist the supervisor's decision — after a REVIEW exit the
        # context consumer reads last_signal to tell the supervisor to write
        # the next TODO (REVIEW-restart branch in api/context.py).
        _write_state(
            project_dir,
            todo_id=current_todo,
            logs_dir=logs_dir,
            phase="verify",
            **({"last_signal": sup_signal} if sup_signal else {}),
        )
        if not sup_signal:
            print(f"ERROR: verify stage produced no supervisor signal for {current_todo}.", file=sys.stderr)
            _log(logs_dir, "verify: empty supervisor signal — pipeline aborted")
            return current_todo, 0, 1

        # U6b: record the acceptance BEFORE the cycle acts on it — the commit
        # gate and the replan live inside the kill window, so the record must
        # already be in state when they start. A kill before the consumption
        # below then leaves a file the next verify can recognize as stale.
        _record_verify_decision(project_dir, current_todo, sup_signal, logs_dir)

        if sup_signal.startswith("REVIEW-"):
            rejected_todo = current_todo  # AUD04-04: the REVIEW belongs to THIS todo
            print(f"Supervisor REJECTED work on {current_todo} (REVIEW signal).", file=sys.stderr)
            _log(logs_dir, f"C1: verify rejected via REVIEW-{current_todo} — replanning")
            # RUN5 #1 (leak-gate): the REVIEW-signal path rejects without
            # api.reject_commit — snapshot the attempt's untracked files
            # here too (best-effort, never fails the cycle).
            from .reject_files import snapshot_rejected_files

            snapshot_rejected_files(project_dir, rejected_todo, logs_dir)
            replan_stage = Stage(name="replan", role="supervisor", kind="replan")
            try:
                replan_signal = _run_supervisor_stage(
                    replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
                )
            except (RuntimeError, TimeoutError) as e:
                print(f"ERROR: replan after REVIEW failed: {e}", file=sys.stderr)
                _consume_verify_decision_guarded(
                    project_dir, rejected_todo, sup_signal, logs_dir, s_name
                )
                return current_todo, 0, 1
            # TODO-0213 (REVIEW fix): the replan answer is the marked TODO's
            # id in the signal — the pre-fix "_find_active_todo" could
            # return a fresh unmarked mid-run dispatch (newest active by
            # construction) instead of the unit the wait picked up. No
            # answer → the rejected unit stays (the pipeline stops anyway).
            new_todo = _replan_answer(replan_signal, current_todo, project_dir)
            if new_todo and new_todo != current_todo:
                current_todo = new_todo
                # Persist replanned TODO so continue resumes the right task.
                _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir)
            # AUD04-04: consume the REVIEW now that the replan had its chance —
            # a re-run of the same TODO must not re-trigger replan on it. The
            # file is keyed to the REJECTED todo (current_todo may have moved
            # to the replanned one by now).
            _consume_verify_decision_guarded(
                project_dir, rejected_todo, sup_signal, logs_dir, s_name
            )
            print("Pipeline stopped: supervisor rejected.", file=sys.stderr)
            return current_todo, 0, 1

        # Approved path: ACK or APPROVE signal
        print(f"Supervisor approved {current_todo} ({sup_signal or 'implicit'}).")
        baseline_sha = _read_baseline_sha(project_dir, current_todo)
        # AUD04-05: the commit gate can time out (APPROVE wait) — treat it as
        # a failed commit (clean stop) instead of a raw traceback.
        try:
            outcome = _maybe_commit(
                s_name, current_todo, stage.on_approved,
                project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha,
            )
        except (RuntimeError, TimeoutError) as e:
            print(f"ERROR: commit gate for {current_todo} crashed. Pipeline stopped.", file=sys.stderr)
            print(f"  Details: {e}", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: commit gate: {e}")
            outcome = _commit_plan.CommitOutcome(_commit_plan.OUTCOME_ERROR, str(e))
        # AUD04-04: consume the accepted ACK/APPROVE now that the commit gate
        # has acted on it — success OR failure. A commit-fail leaves the TODO
        # active; a re-verify of the same TODO must get a FRESH approval, not
        # re-open the gate on this cycle's stale one.
        if not _consume_verify_decision_guarded(
            project_dir, current_todo, sup_signal, logs_dir, s_name
        ):
            # Unsafe managed path — the decision file could not be consumed
            # and the refusal is recorded: stop instead of archiving silently.
            return current_todo, 0, 1
        # R-03: the typed result is handled exactly like on the execute stage.
        if not _commit_outcome_ok(outcome, s_name, current_todo, logs_dir):
            print(
                f"Commit {outcome.status} for {current_todo} — TODO NOT archived, "
                f"changes left in working tree for manual review.",
                file=sys.stderr,
            )
            _log(logs_dir, f"Commit {outcome.status} at verify for {current_todo} — not archived")
            return current_todo, 0, 1
        _mark_plan_step_done(project_dir, current_todo, logs_dir)
        # R2.5: archiving stands on a UnitReceipt — committed, or an
        # explicitly_verified_no_code outcome with its own evidence. A
        # refused/errored publication leaves no receipt, and the mere
        # presence of a DONE file does not authorize the archive. The strict
        # path is the one the receipt exists to block: an EMPTY plan of
        # unknown reason (skip reason ``no changes to commit``) with no
        # no-code receipt. A committed outcome (work already in HEAD) and a
        # policy that never commits (``on_approved: next``) are the legacy
        # carve-out. A present receipt is always enforced by the callee.
        # R2.5 architector-review: the strict path keys on the SHARED
        # ``NO_CHANGES_REASON`` constant the gate emits, not on a copy of the
        # prose — a wording change in the message can no longer silently flip
        # the archive gate open.
        from . import publish as _publish
        allow_no_receipt = not (
            outcome.status == _commit_plan.OUTCOME_SKIPPED
            and outcome.reason == _commit_plan.NO_CHANGES_REASON
        )
        try:
            archived = _publish.archive_after_receipt(
                project_dir, current_todo, allow_no_receipt=allow_no_receipt
            )
        except _publish.PublishError as e:
            print(
                f"Archive refused for {current_todo}: {e} — TODO NOT archived.",
                file=sys.stderr,
            )
            _log(logs_dir, f"R2.5: archive refused for {current_todo}: {e}")
            return current_todo, 0, 1
        if archived:
            _log(logs_dir, f"DF6-1: archived {current_todo} → {archived}")
        return current_todo, 1, 0  # next stage

    # Other supervisor kinds (e.g. replan without verify)
    return current_todo, 1, 0


def _handle_net_death(
    *,
    s_name: str,
    net_retries: int,
    net_limit: int,
    net_key: str,
    project_dir: Path,
    logs_dir: Path,
    outbox: Path,
    todo_id: str,
    prefixes: tuple[str, ...],
) -> tuple[int, bool]:
    """U6b/U6c: what a network-class death does — schedule the backoff
    retry (budget left) or declare the budget exhausted.

    Shared by both death paths (the silent no-signal exit and the
    RuntimeError/TimeoutError crash) so they can never classify or spend
    the retry budget differently. Returns (new_net_retries,
    retry_scheduled); the caller breaks to the net-round epilogue either
    way — scheduled means the next net round, exhausted means the
    failure path.
    """
    import time as _time

    from . import _net
    from .pipeline_state import write_state as _ws

    if net_retries < net_limit:
        backoff = _net.NET_RETRY_BACKOFFS[
            min(net_retries, len(_net.NET_RETRY_BACKOFFS) - 1)
        ]
        # TODO-0235 (REVIEW F7): cleanup first — a refusal cancels the retry,
        # so it must not spend the budget, write state or print "retrying".
        refusal = _clean_signals_guarded(outbox, todo_id, prefixes)
        if refusal is not None:
            # TODO-0235 (REVIEW N1): a refusal here is not a network death —
            # stop the retry budget and let the failure/salvage path run.
            _log(
                logs_dir,
                f"U6b: stale-signal cleanup refused — unsafe managed path: {refusal}",
            )
            return net_retries, False
        net_retries += 1
        _ws(
            project_dir,
            net_retry_count=net_retries, net_retry_key=net_key,
            logs_dir=logs_dir,
        )
        print(
            f"U6b: worker log shows a network failure — retrying "
            f"'{s_name}' in {backoff:.0f}s "
            f"(net retry {net_retries}/{net_limit})...",
            file=sys.stderr,
        )
        _log(
            logs_dir,
            f"U6b: network failure on {s_name} — backoff "
            f"{backoff:.0f}s (net retry {net_retries}/{net_limit})",
        )
        # B2: the backoff pause is downtime — the endpoint is down, no work
        # can happen until it recovers.
        backoff_start = _time.monotonic()
        _time.sleep(backoff)
        from . import run_state as _run_state
        _run_state.add_downtime(
            project_dir, _time.monotonic() - backoff_start,
            reason=f"net-backoff:{todo_id}:{s_name}", logs_dir=logs_dir,
            # ORCH M5.2: land in the run that owns this stage — a service
            # run's backoff must not rewrite the main run.yaml.
            slot=_run_state.run_slot_for_todo(project_dir, todo_id),
        )
        return net_retries, True
    print(
        f"U6b: network retries exhausted ({net_limit}/{net_limit}) "
        f"on '{s_name}' — no more retries",
        file=sys.stderr,
    )
    _log(
        logs_dir,
        f"U6b: network retries exhausted on {s_name} "
        f"(limit {net_limit}) — proceeding to failure path",
    )
    return net_retries, False


def _salvage_attempt(
    project_dir: Path, todo_id: str, stage_name: str
) -> tuple[int, str]:
    """The per-(TODO, stage) salvage counter and its identity key.

    AUD02-07: a different stage or TODO starts at 1 instead of inheriting a
    stale count (no "ATTEMPT 2" on a brand-new stage). TWO places write
    ``salvage_count``/``salvage_count_key`` — the salvage path and the typed
    unsafe-path stop — so the arithmetic lives here once; a second copy is
    how the two counters drift apart.
    """
    state = read_state(project_dir) or {}
    try:
        prev_count = int(state.get("salvage_count", 0) or 0)
    except (TypeError, ValueError):
        prev_count = 0
    key = f"{todo_id}:{stage_name}"
    return (prev_count + 1 if state.get("salvage_count_key") == key else 1), key


def _unsafe_stage_stop(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    logs_dir: Path,
    exc: Exception,
) -> None:
    """TODO-0235 (M3d, REVIEW N1): typed stage stop for a managed refusal.

    A ``ManagedPathError`` from a barrier is not a worker death: retrying
    cannot fix a symlinked ``.agentic`` surface. Left uncaught it escaped to
    the CLI's last-resort handler — one line, no stage record, no salvage
    state, so the supervisor saw a silent pipeline. Record the stage line,
    mark salvage and leave a note; the caller stops the pipeline.
    """
    msg = f"stage {stage_name} refused — unsafe managed path: {exc}"
    print(f"ERROR: {msg}. Pipeline stopped.", file=sys.stderr)
    _log(logs_dir, msg)

    attempt, key = _salvage_attempt(project_dir, todo_id, stage_name)
    _write_state(
        project_dir,
        salvage_needed=True, salvage_stage=stage_name,
        salvage_count=attempt, salvage_count_key=key,
        last_signal=None,
        logs_dir=logs_dir,
    )

    note = paths.inbox(project_dir) / f"SALVAGE-{todo_id}.md"
    body = "\n".join([
        f"# Salvage needed: {stage_name} refused an unsafe managed path",
        "",
        f"**TODO:** {todo_id}",
        f"**Stage:** {stage_name}",
        "",
        "## What happened",
        "",
        "A managed-filesystem barrier refused a destructive operation on a",
        "path under `.agentic`: a component is a symlink (or the platform",
        "has no no-follow primitives). The stage never touched the unsafe",
        "path. Fix the symlink, then retry the stage.",
        "",
        "```",
        str(exc),
        "```",
        "",
    ])
    # The tempting write is the same file the silent-exit prompt writes — one
    # managed writer, never a bare rename onto a possibly symlinked inbox.
    _write_salvage_note(note, body, logs_dir)


def _clean_signals_guarded(
    outbox: Path, todo_id: str, prefixes: tuple[str, ...]
) -> managed_fs.ManagedPathError | None:
    """Run the destructive stale-signal cleanup; return the refusal, if any.

    The retry branches, the net-death handler and the transition consume all
    owe an answer to a barrier refusal, but answer differently (typed stop vs
    spent retry budget). ONE mechanism here, policy at the call site — two
    copies of the same try/except is how the branches drift apart.
    """
    try:
        clean_stage_signals(outbox, todo_id, *prefixes)
    except managed_fs.ManagedPathError as e:
        return e
    return None


def _clean_signals_or_stop(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    logs_dir: Path,
    outbox: Path,
    prefixes: tuple[str, ...],
) -> bool:
    """M3d (REVIEW architect-review): stale-signal cleanup with a typed stop.

    The retry branches of ``execute_agent_stage`` all call the same destructive
    cleanup, so they all owe the same answer to a barrier refusal. Written
    once: a refusal is a stage stop, never a raw traceback. False = stop.
    """
    refusal = _clean_signals_guarded(outbox, todo_id, prefixes)
    if refusal is None:
        return True
    _unsafe_stage_stop(project_dir, todo_id, stage_name, logs_dir, refusal)
    return False


def _stage_allowlist(stage: Stage) -> tuple[str, ...]:
    """SPEC IMP5 (K5): the stage's write allowlist = ``output`` ∪ ``write_allow``.

    Enforcement starts whenever the stage names an ``output`` OR a
    ``write_allow`` (QA-0254-1: an output-only stage was a silent bypass).
    The ONE escape is an explicit, named opt-out declared in the pipeline —
    ``write_check: off`` — carried by the awf-generated service run, whose
    worker writes the role candidate AND project files (see
    ``_write_service_pipeline``). There is no implicit bypass.
    """
    if str(stage.write_check or "").strip().lower() == "off":
        return ()
    if not stage.output and not stage.write_allow:
        return ()
    allowed: list[str] = []
    if stage.output:
        allowed.append(stage.output)
    allowed.extend(stage.write_allow)
    return tuple(allowed)


def _stage_manifest_before(
    project_dir: Path, stage: Stage
) -> dict[str, str] | None:
    """Snapshot the tree before the stage — ``None`` when unenforced.

    AUD4: a snapshot failure is NOT swallowed. The caller turns it into a
    ``BLOCKED`` stage (fail closed) instead of running unfenced.
    """
    if not _stage_allowlist(stage):
        return None
    return stage_manifest.snapshot(project_dir)


def _stage_diff_path(project_dir: Path, todo_id: str, stage_name: str) -> Path:
    """The K5 stage-diff report — ONE name for the writer and the BLOCKED link."""
    return paths.context_dir(project_dir) / f"STAGE-DIFF-{todo_id}-{stage_name}.md"


def _stage_write_violation(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    before: dict[str, str] | None,
    stage: Stage,
) -> list[str]:
    """Paths the stage touched outside its allowlist (K5).

    A violation is recorded in ``context/STAGE-DIFF-<todo>-<stage>.md`` and
    returned; the caller routes the stage to ``BLOCKED`` regardless of the
    worker's own signal. Nothing is rolled back and nothing is committed —
    the file stays exactly where the worker put it. A failed AFTER-snapshot
    raises (AUD4: the caller blocks the stage, never skips the check).

    The report is the human trace; the persistent record the commit gate
    reads is written by the caller. A failed report write therefore must
    NOT escape as a crash: that used to skip ``record_violation`` and free
    the rejected path for a later commit.
    """
    if before is None:
        return []
    allowlist = _stage_allowlist(stage)
    after = stage_manifest.snapshot(project_dir)
    touched = stage_manifest.diff(before, after)
    bad = stage_manifest.violations(touched, allowlist)
    if not bad:
        return []
    report = (
        f"# STAGE-DIFF — {stage_manifest.display_path(todo_id)} / "
        f"{stage_manifest.display_path(stage_name)}\n\n"
        "Пути вне allowlist стадии:\n\n"
        + "".join(f"- `{stage_manifest.display_path(path)}`\n" for path in bad)
        + "\nAllowlist:\n\n"
        + "".join(f"- `{stage_manifest.display_path(path)}`\n" for path in allowlist)
        + "\nСтадия переведена в BLOCKED: файлы остаются, коммита нет.\n"
    )
    context = paths.context_dir(project_dir)
    try:
        context.mkdir(parents=True, exist_ok=True)
        managed_fs.managed_write_text(
            _stage_diff_path(project_dir, todo_id, stage_name),
            report,
            root=project_dir,
        )
    except (OSError, UnicodeError, managed_fs.ManagedPathError) as e:
        # Fail-closed: the violation still returns and the caller writes the
        # record. The report is best-effort; crashing here would drop the
        # record and leave the rejected path committable.
        _log(
            paths.logs_dir(project_dir),
            "STAGE-DIFF report not written: "
            f"{stage_manifest.display_path(str(e))} — the violation is still recorded",
        )
    _log(
        paths.logs_dir(project_dir),
        f"STAGE-DIFF: {stage_name} wrote outside its allowlist: "
        + ", ".join(stage_manifest.display_path(path) for path in bad),
    )
    return bad


def _blocked_note_lines(
    verdict: str,
    findings: list[str],
    directions: list[str],
) -> list[str]:
    """The standard BLOCKED schema in ONE place (``docs/unit-contract.md``).

    All three engine producers — an auth death (R12), a write violation
    (AUD1) and the hard cap (R3, ``_stop_on_hard_timeout``) — pass
    structured sections here, so a heading change cannot land in one
    producer and leave the others unparseable by ``awf blocked-draft``.
    Items are plain text; the ``- `` marker is added here, once.
    """
    return [
        "## ВЕРДИКТ",
        "",
        verdict,
        "",
        "## НАХОДКИ",
        "",
        *(f"- {item}" for item in findings),
        "",
        "## НАПРАВЛЕНИЯ-ФИКСА",
        "",
        *(f"- {item}" for item in directions),
        "",
        "## ПЕРЕНОСЫ",
        "",
        "- нет",
        "",
    ]


def _write_blocked_note(
    project_dir: Path,
    todo_id: str,
    verdict: str,
    findings: list[str],
    directions: list[str],
) -> Exception | None:
    """The ENGINE's writer of the standard BLOCKED closure (K12, AUD2).

    PA3: the closer has its own ``closer_stage._blocked_note``; that stays
    separate. The ENGINE's schema is single-sourced here
    (``_blocked_note_lines``), and a test pins the closer to the same
    headings, so neither writer can drift into a form ``awf blocked-draft``
    cannot parse.

    All three engine producers — an auth death (R12), a write violation
    (AUD1) and the hard cap (R3) — hand their sections to this one writer;
    the schema, the file name, the outbox, the managed barrier and the
    failure log live here, so a change cannot land in one producer only.
    Schema:
    ``docs/unit-contract.md`` (``## ВЕРДИКТ`` / ``## НАХОДКИ`` /
    ``## НАПРАВЛЕНИЯ-ФИКСА`` / ``## ПЕРЕНОСЫ``).

    AUD2: the note and its ``.ready`` marker are written together — the
    same pair the closer writes. ``is_closed``, ``blocked_count`` and the
    wait-for-event reader key off the marker; a note without it left the
    unit "active". Returns the failure (``None`` on success), so each
    producer decides with its own gate in mind: the auth producer refuses a
    BLOCKED state it could not record (AUD5 — no other trace exists); the
    violation producer keeps the closure, because its persistent record,
    not the note, is what the commit gate reads (the closer's policy).
    """
    lines = _blocked_note_lines(verdict, findings, directions)
    outbox = paths.outbox(project_dir)
    # No bare ``mkdir`` here: ``managed_write_text`` creates missing
    # directories through ``mkdirat`` on the verified parent. A path
    # ``mkdir`` before the barrier would follow a symlinked ``.agentic`` /
    # ``outbox`` and create directories OUTSIDE the project before the
    # refusal (audit 0239 / managed_fs doctrine).
    root = managed_fs.agentic_root_for(outbox)
    try:
        managed_fs.managed_write_text(
            outbox / f"BLOCKED-{todo_id}.md",
            "\n".join(lines),
            root=root,
        )
        managed_fs.managed_write_text(
            outbox / f"BLOCKED-{todo_id}.ready",
            "",
            root=root,
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(
            paths.logs_dir(project_dir),
            f"BLOCKED note not written: {stage_manifest.display_path(str(e))}",
        )
        return e
    return None


def _write_stage_blocked_note(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    summary: str,
    leading: list[str],
    closing: str,
    directions: list[str],
    tail: str = "",
) -> Exception | None:
    """K12/K3: the engine's evidence-backed BLOCKED note for a stage death.

    Auth (R12) and the hard cap (R3) differ only in the verdict tail, the
    named findings and the directions. The display paths, the
    ``blocked — стадия ...`` line, the log evidence and the heading schema
    are the same, so they live here: a change cannot land in one producer
    only. AUD4: ``## НАХОДКИ`` quotes ``leading``, the evidence lines, then
    ``closing`` — the note carries the log that explains the death.
    """
    display_todo = stage_manifest.display_path(todo_id)
    display_stage = stage_manifest.display_path(stage_name)
    from . import _net

    evidence = _net.failure_evidence(tail)
    return _write_blocked_note(
        project_dir,
        todo_id,
        f"blocked — стадия `{display_stage}` ({display_todo}): {summary}",
        [
            *leading,
            *(f"лог: `{line}`" for line in evidence),
            closing,
        ],
        directions,
    )


def _write_blocked_signal_note(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    reason: str,
    tail: str = "",
) -> Exception | None:
    """CARRY IMP1 (R12): an auth death becomes the standard BLOCKED note.

    The caller writes ``last_signal=BLOCKED-<id>`` and stops the stage —
    no second spawn.
    """
    return _write_stage_blocked_note(
        project_dir, todo_id, stage_name,
        "провайдер отверг авторизацию (credential/ключ); работа остановлена",
        [reason],
        "повторный спавн не выполнялся: backoff не лечит мёртвый ключ",
        [
            "проверь credential/API-ключ провайдера в opencode.json",
            "после исправления перезапусти стадию (awf continue / awf run-next)",
        ],
        tail,
    )


def _close_stage_blocked(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    logs_dir: Path,
    failure: Exception | None,
    log_line: str,
) -> None:
    """K12/K3: the one closure of every BLOCKED stage stop.

    Auth (R12) and the hard cap (R3) die for different reasons and write
    different notes, but the closure is identical: a failed note write is
    a typed unsafe stop, otherwise the ``BLOCKED-<id>`` marker and one log
    line. Two copies would let the marker policy drift.

    AUD5: when the note cannot be written the stage is NOT marked
    ``BLOCKED`` — a marker with no note would tell the supervisor a unit is
    blocked while ``awf blocked-draft`` has nothing to read. Fall through to
    the same typed salvage stop the managed-path refusals use.
    """
    if failure is not None:
        _unsafe_stage_stop(project_dir, todo_id, stage_name, logs_dir, failure)
        return
    _write_state(
        project_dir,
        last_signal=f"BLOCKED-{todo_id}",
        logs_dir=logs_dir,
    )
    _log(logs_dir, log_line)


def _stop_on_auth_failure(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    logs_dir: Path,
    tail: str = "",
) -> None:
    """CARRY IMP1 (R12): the one auth stop for BOTH death branches.

    A dead credential is not a silent death: the standard BLOCKED closure,
    the ``BLOCKED-<id>`` marker and a stopped stage — no second spawn, no
    silent-retry budget spent. The crash branch and the no-signal branch
    call this; a copied block in one of them would let the two deaths
    drift. The closure itself is shared with the hard cap
    (``_close_stage_blocked``).
    """
    failure = _write_blocked_signal_note(
        project_dir, todo_id, stage_name,
        "лог воркера: авторизация провайдера отклонена (401/403 credential)",
        tail,
    )
    _close_stage_blocked(
        project_dir, todo_id, stage_name, logs_dir, failure,
        f"CARRY IMP1: auth failure on {stage_name} — BLOCKED, stage stopped",
    )


def _stop_on_hard_timeout(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    logs_dir: Path,
    seconds: float,
    tail: str = "",
) -> None:
    """K3 (R3): the hard cap is a BLOCKED stop, not a network retry.

    The cap means the stage hung — the unit is too big or its ``timeout``
    is too tight, and a repeat would burn the same hour again (A4). The
    note is the standard BLOCKED closure so ``awf blocked-draft`` can
    build the REVIEW draft; its findings quote the cap and the log tail.
    The marker half is the shared ``_close_stage_blocked`` (AUD5).
    """
    cap = f"{seconds:g}s"
    failure = _write_stage_blocked_note(
        project_dir, todo_id, stage_name,
        f"жёсткий таймаут {cap}; стадия остановлена без повторного спавна",
        [f"кап: {cap}"],
        "повторный спавн не выполнялся: повтор сжёг бы тот же кап",
        [
            "подними timeout (--timeout / ключ стадии / "
            "automation.stage_timeout_seconds)",
            "раздели юнит на части, если работа не влезает в кап",
        ],
        tail,
    )
    _close_stage_blocked(
        project_dir, todo_id, stage_name, logs_dir, failure,
        f"hard timeout on {stage_name} — BLOCKED, stage stopped",
    )


def _write_blocked_violation_note(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    paths_outside: list[str],
    error: str,
    stage_ran: bool,
) -> None:
    """AUD1: the BLOCKED note the supervisor's replan reads, not a DONE.

    It follows the STANDARD BLOCKED schema (``docs/unit-contract.md``:
    ``## ВЕРДИКТ`` / ``## НАХОДКИ`` / ``## НАПРАВЛЕНИЯ-ФИКСА`` /
    ``## ПЕРЕНОСЫ``), so ``awf blocked-draft`` can build the REVIEW draft
    from it. A free-form note under the same file name was invisible to
    that reader.
    """
    # The note must name the files that really exist: both paths come from
    # the SAME constructors the writers use (``violation_path`` for the
    # record the commit gate reads, ``_stage_diff_path`` for the report).
    # A copied literal is how the remedy drifts from the blocking file.
    root = Path(project_dir).resolve()
    report_file = _stage_diff_path(root, todo_id, stage_name)
    diff_rel = report_file.relative_to(root).as_posix()
    record_rel = (
        stage_manifest.violation_path(root, todo_id).relative_to(root).as_posix()
    )
    display_todo = stage_manifest.display_path(todo_id)
    display_stage = stage_manifest.display_path(stage_name)
    if paths_outside:
        verdict = (
            f"blocked — стадия `{display_stage}` ({display_todo}) записала "
            "пути вне allowlist; файлы остаются, коммита нет"
        )
    else:
        verdict = (
            f"blocked — стадия `{display_stage}` ({display_todo}): снимок "
            f"манифеста недоступен ({error or 'причина не названа'})"
        )
    findings: list[str] = []
    if paths_outside:
        findings += [
            f"`{stage_manifest.display_path(path)}` — вне allowlist стадии"
            for path in paths_outside
        ]
        report_status = "" if report_file.is_file() else " (не записан)"
        findings.append(
            f"отчёт со списком и allowlist: "
            f"`{stage_manifest.display_path(diff_rel)}`{report_status}"
        )
    else:
        findings.append(
            "стадия прошла без забора — её записи считаются непроверенными"
            if stage_ran
            else "стадия не запускалась: без манифеста забор невозможен"
        )
    # The note is the owner-facing trace here; the violation record written
    # by ``_block_on_write_violation`` BEFORE this call is the gate. A
    # failed note write is logged (inside the writer) and the closure
    # stands — same policy as ``closer_stage._blocked_note``.
    _write_blocked_note(
        project_dir,
        todo_id,
        verdict,
        findings,
        [
            "откати или перепиши перечисленные пути (файлы автоматически не трогаются)",
            f"удали `{stage_manifest.display_path(record_rel)}`, чтобы снять нарушение явно",
            "пока запись открыта, commit-план отказывает любому коммиту",
        ],
    )


def _block_on_write_violation(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    stage: Stage,
    paths_outside: list[str],
    error: str,
    logs_dir: Path,
    before: dict[str, str] | None,
    *,
    stage_ran: bool = True,
) -> bool:
    """Open the violation for the commit gate and make it visible (AUD1/AUD2).

    Writes, in this order: the persistent record (AUD2 — the gate reads it),
    the BLOCKED note (AUD1 — the replan reads it), then the ``last_signal``
    marker (AUD1 — wait_event and the dashboard read it). Returns False when
    the record could not be written: then the stage must stop, because a
    commit would no longer be withheld.
    """
    try:
        stage_manifest.record_violation(
            project_dir,
            todo_id,
            stage_name,
            allowlist=_stage_allowlist(stage),
            paths_outside=paths_outside,
            before=before,
            error=error,
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        print(
            f"ERROR: cannot record the write violation for {todo_id}: {e}",
            file=sys.stderr,
        )
        _log(
            logs_dir,
            "K5: violation record not written "
            f"({stage_manifest.display_path(str(e))}) — stage stopped",
        )
        return False
    _write_blocked_violation_note(
        project_dir, todo_id, stage_name, paths_outside, error, stage_ran
    )
    _write_state(
        project_dir,
        last_signal=f"BLOCKED-{todo_id}",
        logs_dir=logs_dir,
    )
    return True


def _worker_exit_code(log_holder: dict[str, str]) -> int | None:
    """The worker's exit code recorded by ``agent_stage`` for THIS attempt.

    CARRY IMP1: the crash branch gets only the exception, so it would
    otherwise classify a death by text alone and miss the PA1 rule (an
    OOM/signal exit outranks auth text). ``None`` = not known (the crash
    happened before the subprocess returned), which keeps the text-only
    behavior.
    """
    raw = log_holder.get("exit_code")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def execute_agent_stage(
    stage: Stage,
    current_todo: str,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
    stages: list,
    stage_idx: int,
    retry_counts: list,
    auto: bool,
    agent_hard_timeout: int | None,
    pipeline_name: str | None = None,
) -> tuple[str, int, int]:
    """Execute one agent stage.

    Returns (new_current_todo, new_stage_idx, exit_code).
    - new_stage_idx: next stage index (may jump for rollback/escalate)
    - exit_code: 0 = success, 1 = stop pipeline
    """
    import time as _time

    from .pipeline_state import write_state as _ws

    s_name = stage.name
    s_kind = stage.kind
    outbox = paths.outbox(project_dir)
    # ORCH M3.2: this stage entry's wall-clock start — the freshness
    # reference for the declared-output check (a leftover from a previous
    # stage/run is older and does not count as this stage's work).
    stage_start_wall = _time.time()

    if not current_todo:
        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print(f"No active TODO for agent stage '{s_name}'. Run supervisor stage first.")
            return current_todo, stage_idx, 1

    # CLOSER IMP2 (K2): the ONE decision point of a stage that declares
    # ``skip_if``. It runs before baseline and manifest: a green verdict must
    # skip without spawning a worker and without leaving a PID; a blocked
    # verdict must stop here with the owner-facing note. A stage without the
    # key keeps the old path and never calls the gate at the boundary.
    if stage.skip_if:
        decision = closer_stage.decide(project_dir, current_todo, stage, config)
        _log(logs_dir, decision.log_line)
        if decision.kind == "blocked":
            # PA2 (TODO-0269): the skip flags are reset at the orchestrator's
            # stage-start write — this branch only records the signal.
            _write_state(
                project_dir,
                last_signal=f"BLOCKED-{current_todo}",
                logs_dir=logs_dir,
            )
            return current_todo, stage_idx, 1
        if decision.kind == "skip":
            # IMP5 (TODO-0304): a green skip of the close stage is the FACT
            # the follow-up check reads. Recorded here, at the decision, so
            # it survives the next stage-start reset of ``stage_skipped``.
            if stage.skip_if == closer_stage.GATE_SKIP_IF:
                closer_stage.record_close_skipped(
                    project_dir, current_todo, skipped=True, logs_dir=logs_dir
                )
            _write_state(
                project_dir,
                stage_skipped=True,
                stage_skip_reason=decision.skip_reason,
                logs_dir=logs_dir,
            )
            _log(logs_dir, f"SKIPPED ({decision.skip_reason})")
            return current_todo, stage_idx + 1, 0
        if stage.skip_if == closer_stage.GATE_SKIP_IF:
            # The closer enters: the follow-up check has a diff to review.
            closer_stage.record_close_skipped(
                project_dir, current_todo, skipped=False, logs_dir=logs_dir
            )

    # OPS IMP1 (R1): ONE resolver for this stage's cap — caller → stage →
    # project → 3600. The line goes to the run log BEFORE the first spawn,
    # so the operator sees the cap and its source in the answer. A bad
    # value normally never reaches here (load error for the stage key, a
    # refusal before the lease for the project key) — the guard keeps the
    # stage from starting silently on a cap nobody validated.
    try:
        stage_cap = _stage_timeout.resolve(agent_hard_timeout, stage.timeout, config)
    except _AwfApiError as e:
        _log(logs_dir, f"STAGE-ERROR: {e}")
        return current_todo, stage_idx, 1
    _log(logs_dir, f"stage timeout: {stage_cap.seconds}s (source: {stage_cap.source})")

    # OPS IMP2 (R2/K2): a full run in the declared checks marks a "big unit"
    # — name it BEFORE the spawn, with the cap and its route. An explicit
    # caller timeout suppresses the line (AC21) at any stage/project keys.
    # The warning never changes rc and never writes BLOCKED; a broken
    # contract is a verify-pack matter, so it builds no line here.
    if agent_hard_timeout is None:
        try:
            contract, _unknown = _unit_contract.parse_todo_contract(
                todos.read_todo_text(project_dir, current_todo)
            )
        except ValueError:
            contract = None
        declared = (contract or {}).get("verify")
        full_cmd = _suite_guard.declares_full_suite(declared, project_dir)
        if full_cmd:
            _log(
                logs_dir,
                f"STAGE-WARN: full suite in verify: `{full_cmd}`; "
                f"cap {stage_cap.seconds}s (source: {stage_cap.source}) — "
                "raise timeout or split the unit",
            )

    _ensure_baseline_sha(project_dir, current_todo, logs_dir)
    _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir, phase="run")

    prev_handoffs = _resolve_prev_handoffs(stages, stage_idx, project_dir, todo_id=current_todo)

    # NEG-4 (day-2 B1): stage-scoped work evidence. A pre-existing diff from an
    # earlier stage must not count as THIS stage's work — otherwise
    # attempt_auto_done synthesizes a false DONE for a worker that wrote
    # nothing. Snapshot the fingerprint before the first attempt.
    stage_baseline_sha = _read_baseline_sha(project_dir, current_todo)
    stage_start_fingerprint = (
        verify.work_fingerprint(project_dir, stage_baseline_sha, current_todo)
        if stage_baseline_sha
        else ""
    )

    # SPEC IMP5 (K5): the write manifest for a stage that declares an
    # allowlist (``output`` ∪ ``write_allow``). Stages without either keep
    # the old behavior. AUD4: a snapshot failure is captured and the stage
    # is BLOCKED, never run with the gate silently off.
    stage_manifest_before: dict[str, str] | None = None
    manifest_error = ""
    # AUD4: the pre-stage snapshot failure is blocked HERE (the worker never
    # runs); the post-stage block below must not repeat it with
    # ``stage_ran=True`` — that note would claim the stage ran unfenced,
    # which it did not.
    manifest_blocked = False
    try:
        stage_manifest_before = _stage_manifest_before(project_dir, stage)
    except RuntimeError as e:
        manifest_error = str(e)
        _log(
            logs_dir,
            f"STAGE-MANIFEST: snapshot unavailable ({e}) — the stage will be BLOCKED",
        )

    # F4: auto-retry transient failures (vllm cold-start, empty output <30s).
    # F7 (dogfood-11): also retry SILENT EXITS — worker exited without a signal
    # AND without work evidence (empty diff). Weak models either answer with
    # text instead of code, or get truncated by their output-token limit
    # mid-reply; both end the opencode loop cleanly. Push the stage to
    # continue with a retry note before falling back to salvage.
    MAX_SILENT_RETRIES = 2
    TRANSIENT_THRESHOLD_SEC = 30

    # U6b: network retry budget. The counter lives in state keyed by
    # (TODO, stage) — same pattern as salvage_count_key: a different stage
    # or TODO starts fresh instead of inheriting a stale count.
    from . import _net

    net_key = f"{current_todo}:{s_name}"
    net_limit = _cfg_int(config, "automation.net_retry_limit", _net.NET_RETRY_LIMIT_DEFAULT)

    # U6b: the counter survives kill+continue like salvage_count (AUD02-07) —
    # a pipeline killed mid net-backoff restarts this stage and must NOT get a
    # fresh budget. Same (TODO, stage) key → inherit; other key/absent → 0.
    _prev_net_state = read_state(project_dir) or {}
    if _prev_net_state.get("net_retry_key") != net_key:
        net_retries = 0
    else:
        try:
            net_retries = int(_prev_net_state.get("net_retry_count", 0) or 0)
        except (TypeError, ValueError):
            net_retries = 0

    # TODO-0196: the silent-exit-WITH-WORK budget — the worker left a diff
    # but no signal. A continue-push ("the work is already in the tree —
    # do not redo it, finish with the signal") instead of an instant
    # salvage. Separate from the no-work silent budget above; persisted in
    # state like the net counter (keyed by (TODO, stage)) so a kill+continue
    # does not hand out a fresh budget. The attempt loop stays the hard cap.
    # TODO-0226: default 2 — the 1st retry is a plain continue-push, the
    # 2nd carries the reduce-pass line (no full suite / run-all), then
    # salvage. One retry was not enough to tell "finish it" from "the
    # worker keeps dying the same way".
    SILENT_WORK_LIMIT = _cfg_int(config, "retry.silent_work_attempts", 2)
    if _prev_net_state.get("sw_retry_key") != net_key:
        sw_retries = 0
    else:
        try:
            sw_retries = int(_prev_net_state.get("sw_retry_count", 0) or 0)
        except (TypeError, ValueError):
            sw_retries = 0

    log_holder: dict[str, str] = {}
    signal = None
    # U6c: bound BEFORE the attempt loop — the crash path (except branch)
    # exits the loop before the in-loop assignment, and the salvage path
    # below still needs the prefixes.
    prefixes = expected_signal_prefixes(s_kind)
    worker_run = 0  # cumulative worker run number (handoff fact "worker run")
    # TODO-0196: the retry note is decided at the retry point (the no-work
    # and the with-work paths push different texts) and consumed by the
    # next iteration. Always None at loop breaks — a decision sets it
    # immediately before `continue`.
    pending_note: str | None = None
    # AUD4: without a pre-stage snapshot the stage must not run unfenced —
    # an empty round list skips the worker and lands on the BLOCKED branch
    # below (a signal is synthesized so the transition still dispatches).
    net_rounds = range(0) if manifest_error else range(net_limit + 1)
    for _net_round in net_rounds:
        net_retry_scheduled = False
        for attempt_no in range(MAX_SILENT_RETRIES + 1):
            import time as _time
            agent_start = _time.monotonic()
            retry_note = pending_note
            pending_note = None
            worker_run += 1
            try:
                _run_agent_stage(stage, current_todo, project_dir, config, logs_dir,
                                 prev_handoffs=prev_handoffs, hard_timeout=stage_cap.seconds,
                                 retry_note=retry_note, attempt=worker_run,
                                 log_holder=log_holder)
            except managed_fs.ManagedPathError as e:
                # TODO-0235 (REVIEW N1): a barrier refusal stops the stage
                # with a record and a salvage note — never a raw escape.
                _unsafe_stage_stop(project_dir, current_todo, s_name, logs_dir, e)
                return current_todo, stage_idx, 1
            except (RuntimeError, TimeoutError) as e:
                # U6c: a crash is not automatically a non-network death —
                # U6b turns "no signal + rc!=0" into a RuntimeError, so the
                # most common network death (endpoint down mid-run) used to
                # stop the pipeline here, never reaching the classification
                # below. Classify the same way as the no-signal path: this
                # run's log tail, plus the preflight-timeout marker in the
                # exception text (the endpoint was down before the worker
                # even started — no log to read yet).
                # CARRY IMP1 (R1): the same "tail only without a signal"
                # invariant the normal exit path holds. A crash AFTER the
                # worker wrote its signal (e.g. the handoff write failed) is
                # not a provider death — classifying its unrelated log text
                # could BLOCK a completed unit. With a signal on disk the
                # crash falls through to the generic stop below.
                signal_on_disk = read_signal_for_todo(
                    outbox, current_todo, *prefixes
                )
                if signal_on_disk:
                    tail = ""
                else:
                    tail = worker_log_tail(log_holder)
                # K3 (R3): the hard cap is not classified by the tail — a
                # stuck stage is a BLOCKED stop (no second spawn). With a
                # signal on disk the pre-existing branch stands (the watcher
                # returns the late signal instead of raising — AC10, INV1).
                # Preflight and the watchdog raise plain ``TimeoutError``
                # (INV3, INV9) and keep their own branches below.
                if isinstance(e, _HardTimeoutError) and not signal_on_disk:
                    _stop_on_hard_timeout(
                        project_dir, current_todo, s_name, logs_dir,
                        e.seconds, tail,
                    )
                    return current_todo, stage_idx, 1
                if signal_on_disk:
                    failure_class = ""
                else:
                    failure_class = _net.classify_failure(
                        tail, _worker_exit_code(log_holder)
                    )
                # QA (U6c): the preflight text check applies ONLY to
                # TimeoutError — preflight is the only network death with no
                # worker log. U6b's RuntimeError embeds the full Cmd (stage
                # and agent names, file paths, prompt), so a project's own
                # "preflight" stage name must not classify a clean-log crash
                # as network. A crash death is classified by its log tail.
                # Pinned by test_crash_text_with_preflight_word_not_network.
                if (
                    not signal_on_disk
                    and isinstance(e, TimeoutError)
                    and _net.is_preflight_timeout(str(e))
                ):
                    failure_class = "network"
                if failure_class in _net.RETRY_FAILURE_CLASSES:
                    net_retries, net_retry_scheduled = _handle_net_death(
                        s_name=s_name, net_retries=net_retries,
                        net_limit=net_limit, net_key=net_key,
                        project_dir=project_dir, logs_dir=logs_dir,
                        outbox=outbox, todo_id=current_todo,
                        prefixes=prefixes,
                    )
                    break  # scheduled → next net round; exhausted → failure path
                if failure_class == "auth":
                    # CARRY IMP1 (R12): a dead credential is not a silent
                    # death — BLOCKED note, state marker, stopped stage.
                    _stop_on_auth_failure(
                        project_dir, current_todo, s_name, logs_dir, tail,
                    )
                    return current_todo, stage_idx, 1
                print(f"ERROR: agent stage '{s_name}' crashed. Pipeline stopped.", file=sys.stderr)
                _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
                return current_todo, stage_idx, 1
            agent_elapsed = _time.monotonic() - agent_start

            signal = read_signal_for_todo(outbox, current_todo, *prefixes)

            if not signal:
                try:
                    signal = wait_for_signal(outbox, current_todo, *prefixes, timeout=30)
                except TimeoutError:
                    pass

            # NEG-4: did THIS stage entry produce changes? (not a foreign diff
            # left over from an earlier stage)
            stage_produced_work = bool(stage_start_fingerprint) and (
                verify.work_fingerprint(project_dir, stage_baseline_sha, current_todo)
                != stage_start_fingerprint
            )

            if not signal:
                if stage_produced_work and verify.attempt_auto_done(
                    project_dir, current_todo, config, stage_baseline_sha
                ):
                    signal = read_signal_for_todo(outbox, current_todo, *prefixes)

            if signal:
                break

            # U6b: worker died without a signal — classify the death BEFORE
            # the silent retry: a dead model endpoint is not fixed by a
            # continue-push, it gets its own backoff-retry budget (30/60/120s).
            # CARRY IMP1 (R12): auth is a third class — a dead credential is
            # a BLOCKED stop, not a silent retry; 5xx/429 share the backoff.
            tail = worker_log_tail(log_holder)
            failure_class = _net.classify_failure(
                tail, _worker_exit_code(log_holder)
            )
            if failure_class in _net.RETRY_FAILURE_CLASSES:
                net_retries, net_retry_scheduled = _handle_net_death(
                    s_name=s_name, net_retries=net_retries,
                    net_limit=net_limit, net_key=net_key,
                    project_dir=project_dir, logs_dir=logs_dir,
                    outbox=outbox, todo_id=current_todo,
                    prefixes=prefixes,
                )
                break  # scheduled → next net round; exhausted → failure path
            if failure_class == "auth":
                _stop_on_auth_failure(
                    project_dir, current_todo, s_name, logs_dir, tail,
                )
                return current_todo, stage_idx, 1

            # TODO-0196: no signal, but the stage LEFT WORK in the tree —
            # only the finish (the signal) is missing. A continue-push with
            # its own budget (retry.silent_work_attempts) instead of an
            # instant salvage: the retry continues the work instead of
            # redoing it. After the budget — the salvage path below, as
            # before. (Net deaths above are classified first and keep their
            # own budget — unchanged.)
            worker_has_work = stage_produced_work
            if worker_has_work and sw_retries < SILENT_WORK_LIMIT:
                # TOCTOU guard (same as the no-work retry): a signal may
                # have landed between the wait timeout above and this
                # decision — a valid signal must never be deleted by the
                # retry cleanup.
                signal = read_signal_for_todo(outbox, current_todo, *prefixes)
                if signal:
                    break
                sw_retries += 1
                print(
                    f"Worker exited with work in the tree but no signal — "
                    f"continue-push retry ({sw_retries}/{SILENT_WORK_LIMIT})...",
                    file=sys.stderr,
                )
                _log(logs_dir, f"TODO-0196: silent exit WITH work — continue-push "
                    f"retry {sw_retries}/{SILENT_WORK_LIMIT} for {s_name} "
                    f"(elapsed={agent_elapsed:.0f}s, diff in tree, no signal)")
                _ws(project_dir, sw_retry_count=sw_retries, sw_retry_key=net_key,
                    logs_dir=logs_dir)
                pending_note = _silent_work_retry_note(
                    sw_retries, current_todo,
                    progress=_progress_block(
                        project_dir, log_holder, current_todo, outbox,
                    ),
                )
                if not _clean_signals_or_stop(
                    project_dir, current_todo, s_name, logs_dir, outbox,
                    expected_signal_prefixes(s_kind),
                ):
                    return current_todo, stage_idx, 1
                continue

            # dogfood-11: no signal — retry only when the worker left NO work
            # evidence (the work-present case takes the continue-push retry
            # above). A rerun of an empty stage risks nothing.
            if attempt_no < MAX_SILENT_RETRIES and not worker_has_work:
                # TOCTOU guard (dogfood-11): a signal may have landed between the
                # wait timeout above and this cleanup (e.g. the worker wrote DONE
                # right at the boundary). Re-read before wiping anything — a
                # valid signal must never be deleted by the retry cleanup.
                signal = read_signal_for_todo(outbox, current_todo, *prefixes)
                if signal:
                    break
                transient = agent_elapsed < TRANSIENT_THRESHOLD_SEC
                kind_hint = "fast exit (transient?)" if transient else "no code, no signal"
                print(
                    f"Worker exited in {agent_elapsed:.0f}s with no signal ({kind_hint}) — "
                    f"retrying with a continue-push ({attempt_no + 1}/{MAX_SILENT_RETRIES})...",
                    file=sys.stderr,
                )
                _log(logs_dir, f"F4/F7: auto-retry {attempt_no + 1}/{MAX_SILENT_RETRIES} "
                    f"for {s_name} (elapsed={agent_elapsed:.0f}s, no signal, no work)")
                if not _clean_signals_or_stop(
                    project_dir, current_todo, s_name, logs_dir, outbox,
                    expected_signal_prefixes(s_kind),
                ):
                    return current_todo, stage_idx, 1
                pending_note = _silent_retry_note(attempt_no + 1, current_todo)
                continue

            break

        if signal:
            break
        if net_retry_scheduled:
            continue  # next net round — the endpoint had a chance to recover
        break

    if manifest_error:
        # AUD4: no pre-stage snapshot — block instead of running unfenced.
        if not _block_on_write_violation(
            project_dir, current_todo, s_name, stage, [], manifest_error,
            logs_dir, None, stage_ran=False,
        ):
            return current_todo, stage_idx, 1
        manifest_blocked = True
        print(
            f"Stage '{s_name}': write manifest unavailable ({manifest_error}) "
            "— routing as blocked.",
            file=sys.stderr,
        )
        signal = f"BLOCKED-{current_todo}"

    signal_from_salvage = False
    if not signal:
        # Salvage path
        baseline_sha = stage_baseline_sha
        # B2: death-detection moment — from here to the written salvage
        # prompt is the measurable part of the salvage handling (log
        # classification, git diff collection, prompt write). The worker's
        # own run above was real work time and is NOT counted.
        import time as _time
        salvage_detect = _time.monotonic()
        print(f"WARNING: No signal after agent stage '{s_name}'.", file=sys.stderr)
        _log(logs_dir, f"No signal after {s_name} — salvage path")

        # Dogfood-11: count consecutive silent exits for this stage. A repeat
        # salvage means retrying the same scope won't help — the SALVAGE note
        # escalates to task splitting / incremental writes instead.
        # AUD02-07: the count is per (TODO, stage) — a different stage or TODO
        # starts fresh at 1 instead of inheriting a stale counter ("ATTEMPT 2"
        # on the first failure of a brand-new stage). The arithmetic is shared
        # with the typed unsafe-path stop (``_salvage_attempt``).
        attempt, salvage_key = _salvage_attempt(project_dir, current_todo, s_name)

        _write_salvage_prompt(
            project_dir, current_todo, s_name, baseline_sha, logs_dir,
            attempt=attempt, auto_retries=attempt_no,
        )
        # B2: the salvage window is over — the prompt is written.
        from . import run_state as _run_state
        _run_state.add_downtime(
            project_dir, _time.monotonic() - salvage_detect,
            reason=f"salvage:{current_todo}:{s_name}", logs_dir=logs_dir,
            # ORCH M5.2: land in the run that owns this stage — a service
            # run's salvage window must not rewrite the main run.yaml.
            slot=_run_state.run_slot_for_todo(project_dir, current_todo),
        )
        _ws(
            project_dir,
            salvage_needed=True, salvage_stage=s_name, salvage_count=attempt,
            # AUD04-08: identity of the counted (TODO, stage) — written by the
            # engine, cleared by no one on stage start, so the count survives
            # kill+continue (the retry cycle) and the dashboard flag reset.
            salvage_count_key=salvage_key,
            # AUD02-01: the worker produced no signal — clear any stale one,
            # so readers never attribute a previous stage's signal to this run.
            last_signal=None,
            logs_dir=logs_dir,
        )

        if auto:
            if baseline_sha and verify.detect_work_evidence(project_dir, baseline_sha, current_todo):
                print(f"  Worker left changes. {current_todo} left ACTIVE for manual salvage.", file=sys.stderr)
                _log(logs_dir, f"Auto: work detected; {current_todo} left active")
            else:
                print("  No worker changes and no signal — failure.", file=sys.stderr)
                _log(logs_dir, f"Auto: no work + no signal — stop at {s_name}")
            return current_todo, stage_idx, 1

        salvage_stage = Stage(name="salvage", role="supervisor", kind="salvage")
        # AUD04-05: a salvage wait timeout used to escape as a raw traceback
        # mid-background-run — same class of event as the main supervisor
        # stage, same clean stop.
        try:
            _run_supervisor_stage(
                salvage_stage, current_todo, auto=False,
                project_dir=project_dir, logs_dir=logs_dir, pipeline_name=pipeline_name
            )
        except (RuntimeError, TimeoutError) as e:
            print("ERROR: supervisor salvage crashed. Pipeline stopped.", file=sys.stderr)
            print(f"  Details: {e}", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: salvage crashed: {e}")
            return current_todo, stage_idx, 1
        signal = read_signal_for_todo(outbox, current_todo, *prefixes)
        if not signal:
            print("No signal after supervisor salvage. Stopping.", file=sys.stderr)
            _log(logs_dir, f"Stopped: salvage produced no signal at {s_name}")
            return current_todo, stage_idx, 1
        # CLOSER IMP4 (K4): a signal the supervisor produced after the
        # worker died without a verdict is not a completed closer run — it
        # must not spend an attempt (AC11).
        signal_from_salvage = True
        print(f"Salvaged signal: {signal}", file=sys.stderr)
        _log(logs_dir, f"Salvaged signal: {signal} via supervisor")

    # Signal received — classify and dispatch transition
    sig_type = signal_type(signal)
    print(f"Signal classified as: {sig_type}")
    # Dogfood-11: stage resolved — reset the consecutive-salvage counter.
    # AUD02-01: record the signal — readers (wait_for_event blocked-wake-up,
    # dashboard banner, context REVIEW-restart hint) all key off this field.
    _write_state(
        project_dir, salvage_count=0, salvage_count_key=None,
        net_retry_count=0, net_retry_key=None,
        last_signal=signal, logs_dir=logs_dir,
    )

    # NEG-4 (day-2 B1): consume the fired .ready signal now that the stage is
    # resolved. The same filename used to stay valid for every later stage —
    # a stale DONE could satisfy the next transition. Keep the .md report as
    # evidence for the handoff.
    outbox_root = managed_fs.agentic_root_for(outbox)
    consumed_leaves = [
        outbox / f"{signal}{suffix}" for suffix in (".ready", ".md.ready")
    ]
    # REVIEW N2's rule applied here (architect-review): EVERY leaf is checked
    # before the FIRST unlink, so a refusal leaves both files untouched instead
    # of consuming .ready and then stopping on .md.ready. A barrier refusal is
    # a typed stage stop, never a raw escape (REVIEW N1).
    try:
        managed_fs.require_posix("consume stage signal")
        for consumed in consumed_leaves:
            managed_fs.assert_no_symlink_escape(consumed, root=outbox_root)
    except managed_fs.ManagedPathError as e:
        _unsafe_stage_stop(project_dir, current_todo, s_name, logs_dir, e)
        return current_todo, stage_idx, 1
    for consumed in consumed_leaves:
        try:
            consumed.unlink()
        except FileNotFoundError:
            pass

    # CLOSER IMP4 (K4): the gate is recomputed on the tree the stage
    # produced (the result check, NOT a second decision point); its colour
    # decides the next step, not the worker's signal type. One path for both
    # signals: a completed run with a verdict charges an attempt, a salvaged
    # signal is re-checked without charging (AC11).
    # A pre-stage manifest failure blocks before the worker ever runs;
    # there is no signal of the closer to settle (manifest_blocked).
    # IMP5 (TODO-0304): only the GATE-driven skip settles a closer run; the
    # follow-up check stage reads the recorded fact and never re-runs the
    # checklist gate here.
    if stage.skip_if == closer_stage.GATE_SKIP_IF and not manifest_blocked:
        outcome = closer_stage.settle_run(
            project_dir,
            current_todo,
            config,
            charge=not signal_from_salvage,
        )
        _log(logs_dir, outcome.log_line)
        if outcome.kind == "blocked":
            _write_state(
                project_dir,
                last_signal=f"BLOCKED-{current_todo}",
                logs_dir=logs_dir,
            )
            return current_todo, stage_idx, 1
        if outcome.kind == "retry":
            # Red and still under the cap: re-enter the same stage. Its entry
            # (``decide``) re-reads the counter. No ``retry_counts`` bump — a
            # planned closer attempt is not a stage failure.
            print(
                f"Closer still red after attempt {outcome.attempts}; "
                "re-entering the stage.",
                file=sys.stderr,
            )
            return current_todo, stage_idx, 0
        # Green: the verdict decides the outcome (a BLOCKED signal after a
        # green gate is a completed close).
        sig_type = "done"

    # SPEC IMP5 (K5): a stage that wrote outside its allowlist is BLOCKED,
    # whatever signal it left. The report names the paths; the DONE signal
    # cannot override it. AUD1: the cause must reach the supervisor — the
    # BLOCKED note, the STAGE-DIFF link and the last_signal marker are
    # written BEFORE the replan runs. AUD4: a failed after-snapshot is the
    # same block, not a skipped check. AUD2: the persistent record makes
    # the commit gate refuse until the path is rolled back or the record
    # is removed explicitly.
    write_violations: list[str] = []
    if not manifest_error:
        try:
            write_violations = _stage_write_violation(
                project_dir, current_todo, s_name, stage_manifest_before, stage
            )
        except RuntimeError as e:
            manifest_error = str(e)
            print(
                f"Stage '{s_name}': write manifest unavailable ({e}) "
                "— routing as blocked.",
                file=sys.stderr,
            )
    if write_violations or (manifest_error and not manifest_blocked):
        if manifest_error:
            _log(logs_dir, f"STAGE-MANIFEST: after-snapshot unavailable ({manifest_error})")
        if write_violations:
            print(
                f"Stage '{s_name}' wrote outside its allowlist: "
                f"{', '.join(write_violations)} — routing as blocked.",
                file=sys.stderr,
            )
        if not _block_on_write_violation(
            project_dir, current_todo, s_name, stage, write_violations,
            manifest_error, logs_dir, stage_manifest_before,
        ):
            return current_todo, stage_idx, 1
        sig_type = "blocked"

    # ORCH M3.2: a DONE signal is not the stage's contract — its declared
    # output is. A declared output that is missing or stale means the
    # stage did not fulfill its assignment: route it through the stage's
    # normal failure path (the on_blocked policy: escalate/stop/rollback),
    # never a silent advance. The message names the stage and the
    # expected output. Stages without a declared output keep the previous
    # behavior (back-compat).
    if sig_type in ("done", "approved") and stage.output:
        problem = _declared_output_problem(
            project_dir, stage.output, stage_start_wall
        )
        if problem:
            print(
                f"Stage '{s_name}' did not fulfill its assignment: "
                f"{problem} — routed through the stage failure path.",
                file=sys.stderr,
            )
            _log(
                logs_dir,
                f"ORCH M3.2: stage {s_name} output check failed: {problem} "
                "— routed as blocked (stage policy)",
            )
            sig_type = "blocked"

    # SPEC: the requirements-review report is itself a verdict. A fresh
    # report that says ``Статус: FAIL`` rejects the stage — the engine
    # rewinds to the author without a supervisor replan (the DONE signal
    # cannot override the document). PASS keeps the normal advance.
    auto_rollback = False
    if sig_type in ("done", "approved") and stage.output:
        verdict = _declared_review_verdict(project_dir, stage.output)
        if verdict:
            print(
                f"Stage '{s_name}': declared output rejects the stage "
                "(Статус: FAIL or BLOCKED) — routing as rejected "
                "(automatic rollback)."
            )
            _log(
                logs_dir,
                f"SPEC: stage {s_name} output is a rejecting verdict "
                "(FAIL/BLOCKED) — routed as rejected",
            )
            sig_type = verdict
            auto_rollback = True

    action, target = resolve_transition(stage, sig_type)
    # S14: a BLOCKED readiness report is an automatic rewind to the AUTHOR
    # who owns the finding (requirements or architecture) — the policy
    # gives the rollback action, the report's owner picks the stage.
    if auto_rollback and action == "rollback":
        author_stage = _readiness_rollback_target(
            project_dir, stage.output or "", stages
        )
        if author_stage:
            target = author_stage
    _log(logs_dir, f"Transition: stage={stage_idx} signal={sig_type} -> action={action} target={target}")

    if action in ("next", "commit_and_next", "commit_and_report"):
        # AUD04-05: the commit gate can time out (APPROVE wait) — stop
        # cleanly instead of letting the TimeoutError traceback out.
        try:
            new_idx, rc = _handle_next(
                project_dir, logs_dir, s_name, current_todo, action, auto, retry_counts, stage_idx,
            )
        except (RuntimeError, TimeoutError) as e:
            print(f"ERROR: commit gate at '{s_name}' crashed. Pipeline stopped.", file=sys.stderr)
            print(f"  Details: {e}", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: commit gate: {e}")
            return current_todo, stage_idx, 1
        # R-03: a refused/failed commit on the execute stage stops the
        # cycle too (A-14) — the TODO stays active, the stage does not
        # advance.
        return current_todo, new_idx, rc

    elif action == "escalate":
        new_idx, new_todo, exit_code = _handle_escalate(
            project_dir, logs_dir, s_name, current_todo, auto, stage, retry_counts, stage_idx,
            pipeline_name,
        )
        return new_todo, new_idx, exit_code

    elif action == "rollback":
        if _find_stage_index(stages, target) < 0:
            # NEG-2 (dogfood-11): a rollback target that doesn't exist (typo,
            # or a pipeline generated with the old default) must not hard-stop
            # the pipeline — escalate to the supervisor instead, same recovery
            # path as BLOCKED. The loader warns about such targets up front.
            print(
                f"WARNING: rollback target '{target}' not found in pipeline — "
                f"escalating to supervisor instead.",
                file=sys.stderr,
            )
            _log(logs_dir, f"Rollback target {target!r} not found; escalating to supervisor")
            new_idx, new_todo, exit_code = _handle_escalate(
                project_dir, logs_dir, s_name, current_todo, auto, stage,
                retry_counts, stage_idx, pipeline_name,
            )
            return new_todo, new_idx, exit_code
        # SPEC: an automatic FAIL rewind is the report's own decision — the
        # same unit goes back to the author stage without a supervisor
        # replan. A worker-emitted REVIEW-REJECTED keeps the replan path.
        if auto_rollback:
            new_idx, new_todo, exit_code = _handle_direct_rollback(
                project_dir, logs_dir, stages, current_todo, target,
                source=s_name,
            )
            return new_todo, new_idx, exit_code
        new_idx, new_todo, exit_code = _handle_rollback(
            project_dir, logs_dir, stages, current_todo, auto, target, pipeline_name,
            source=s_name,
        )
        return new_todo, new_idx, exit_code

    elif action == "stop":
        print("Pipeline stopped by policy.")
        _log(logs_dir, f"Pipeline stopped by policy at stage {s_name}")
        # AUD04-02: rc=1 so the orchestrator exits — rc=0 with the same
        # stage_idx used to re-run this stage forever (the BLOCKED-stop loop).
        return current_todo, stage_idx, 1

    # AUD04-12: resolve_transition returns a closed action set
    # (next/commit_and_next/commit_and_report/escalate/rollback/stop) and
    # every member is handled above — the old `else: Unknown transition`
    # branch was unreachable dead code. The invariant is pinned explicitly:
    # a new resolver action must be dispatched here, not silently stopped.
    raise AssertionError(
        f"Unhandled transition action {action!r} at stage {s_name!r} — "
        "resolve_transition returned an action the dispatcher does not handle"
    )
