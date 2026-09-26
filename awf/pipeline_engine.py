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

from . import commit_plan as _commit_plan
from . import config as cfg_mod
from . import paths, todos, verify
from ._log import log as _log
from .agent_stage import resolve_prev_handoffs as _resolve_prev_handoffs
from .agent_stage import run_agent_stage as _run_agent_stage
from .commit_gate import maybe_commit as _maybe_commit
from .pipeline import Stage
from .pipeline_state import read_state
from .pipeline_state import write_state as _write_state
from .plan_progress import mark_plan_step_done as _mark_plan_step_done
from .signals import expected_signal_prefixes, read_signal_for_todo, signal_type, wait_for_signal
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


def _unblock_todo(project_dir: Path, todo_id: str, logs_dir: Path) -> None:
    """Day-2 B2: clear the BLOCKED closure so the TODO counts as active again.

    Moves ``BLOCKED-{todo}.{ready,md}`` (canonical + legacy short form) from
    outbox to ``.agentic/context/`` as evidence — mirrors the manual recovery
    the user had to perform before this existed.
    """
    from .signals import short_id

    outbox = paths.outbox(project_dir)
    ctx = paths.context_dir(project_dir)
    ctx.mkdir(parents=True, exist_ok=True)
    moved = False
    for candidate_id in {todo_id, short_id(todo_id)}:
        for ext in (".ready", ".md"):
            src = outbox / f"BLOCKED-{candidate_id}{ext}"
            if src.exists():
                try:
                    src.replace(ctx / src.name)
                    moved = True
                except OSError:
                    pass
    if moved:
        _log(logs_dir, f"Day-2 B2: BLOCKED closure moved to context/ for {todo_id}")


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
    """Transition: BLOCKED → supervisor replan + retry same stage.

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

    new_todo = _find_active_todo(project_dir)
    if not new_todo:
        print("Supervisor did not create a new TODO and left no ACK. Stopping.")
        return stage_idx, current_todo, 1
    print(f"New TODO: {new_todo} — retrying stage '{s_name}'")
    return stage_idx, new_todo, 0  # stay on same stage_idx


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

    # AUD03-01: rollback budget. The key is per (TODO, source->target), so a
    # replanned TODO or a different rollback route gets a fresh budget.
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
        return -1, current_todo, 1

    counts[rb_key] = rb_count + 1
    _write_state(project_dir, rollback_counts=counts, logs_dir=logs_dir)
    print(f"Rolling back to stage: {stages[target_idx].name} (rollback {rb_count + 1}/{limit})")
    _log(
        logs_dir,
        f"Rollback to stage {stages[target_idx].name} (index {target_idx}, {rb_count + 1}/{limit})",
    )

    replan_stage = Stage(name="replan", role="supervisor", kind="replan")
    # AUD04-05: same clean stop as the main supervisor stage — a replan
    # timeout/crash must not escape as a raw traceback.
    try:
        _run_supervisor_stage(
            replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
        )
    except (RuntimeError, TimeoutError) as e:
        print("ERROR: supervisor replan after rollback crashed. Pipeline stopped.", file=sys.stderr)
        print(f"  Details: {e}", file=sys.stderr)
        _log(logs_dir, f"Pipeline stopped at stage {source or target}: rollback replan crashed: {e}")
        return -1, current_todo, 1

    new_todo = _find_active_todo(project_dir)
    if not new_todo:
        print("Rollback: supervisor did not create a new TODO. Stopping.", file=sys.stderr)
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
    from ._atomic import atomic_write_text

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
                capture_output=True, text=True, check=False, timeout=10,
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

    atomic_write_text(salvage_file, "\n".join(parts))
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
                _run_supervisor_stage(
                    replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
                )
            except (RuntimeError, TimeoutError) as e:
                print(f"ERROR: replan after REVIEW failed: {e}", file=sys.stderr)
                _consume_verify_decision(project_dir, rejected_todo, sup_signal, logs_dir)
                return current_todo, 0, 1
            new_todo = _find_active_todo(project_dir)
            if new_todo and new_todo != current_todo:
                current_todo = new_todo
                # Persist replanned TODO so continue resumes the right task.
                _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir)
            # AUD04-04: consume the REVIEW now that the replan had its chance —
            # a re-run of the same TODO must not re-trigger replan on it. The
            # file is keyed to the REJECTED todo (current_todo may have moved
            # to the replanned one by now).
            _consume_verify_decision(project_dir, rejected_todo, sup_signal, logs_dir)
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
        _consume_verify_decision(project_dir, current_todo, sup_signal, logs_dir)
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
        from .todos import archive_todo
        archived = archive_todo(project_dir, current_todo)
        if archived:
            _log(logs_dir, f"DF6-1: archived {current_todo} → {archived}")
        return current_todo, 1, 0  # next stage

    # Other supervisor kinds (e.g. replan without verify)
    return current_todo, 1, 0


def _death_tail(log_holder: dict[str, str]) -> str:
    """U6b/U6c: this run's worker-log tail (from its start offset).

    Append-mode log (dogfood-11): without the offset an earlier run's
    network marker would leak into this run's death classification
    (QA TODO-0017). '' when the run never got a log (the preflight died
    before the file was created).
    """
    from . import _net

    log_path_str = log_holder.get("log_path", "")
    if not log_path_str:
        return ""
    start_offset = int(log_holder.get("log_start_offset") or 0)
    return _net.read_log_tail(Path(log_path_str), start_offset=start_offset)


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
    from .signals import clean_stage_signals

    if net_retries < net_limit:
        net_retries += 1
        backoff = _net.NET_RETRY_BACKOFFS[
            min(net_retries - 1, len(_net.NET_RETRY_BACKOFFS) - 1)
        ]
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
        clean_stage_signals(outbox, todo_id, *prefixes)
        # B2: the backoff pause is downtime — the endpoint is down, no work
        # can happen until it recovers.
        backoff_start = _time.monotonic()
        _time.sleep(backoff)
        from . import run_state as _run_state
        _run_state.add_downtime(
            project_dir, _time.monotonic() - backoff_start,
            reason=f"net-backoff:{todo_id}:{s_name}", logs_dir=logs_dir,
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
    from .pipeline_state import write_state as _ws

    s_name = stage.name
    s_kind = stage.kind
    outbox = paths.outbox(project_dir)

    if not current_todo:
        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print(f"No active TODO for agent stage '{s_name}'. Run supervisor stage first.")
            return current_todo, stage_idx, 1

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

    log_holder: dict[str, str] = {}
    signal = None
    # U6c: bound BEFORE the attempt loop — the crash path (except branch)
    # exits the loop before the in-loop assignment, and the salvage path
    # below still needs the prefixes.
    prefixes = expected_signal_prefixes(s_kind)
    worker_run = 0  # cumulative worker run number (handoff fact "worker run")
    for _net_round in range(net_limit + 1):
        net_retry_scheduled = False
        for attempt_no in range(MAX_SILENT_RETRIES + 1):
            import time as _time
            agent_start = _time.monotonic()
            retry_note = _silent_retry_note(attempt_no, current_todo) if attempt_no else None
            worker_run += 1
            try:
                _run_agent_stage(stage, current_todo, project_dir, config, logs_dir,
                                 prev_handoffs=prev_handoffs, hard_timeout=agent_hard_timeout,
                                 retry_note=retry_note, attempt=worker_run,
                                 log_holder=log_holder)
            except (RuntimeError, TimeoutError) as e:
                # U6c: a crash is not automatically a non-network death —
                # U6b turns "no signal + rc!=0" into a RuntimeError, so the
                # most common network death (endpoint down mid-run) used to
                # stop the pipeline here, never reaching the classification
                # below. Classify the same way as the no-signal path: this
                # run's log tail, plus the preflight-timeout marker in the
                # exception text (the endpoint was down before the worker
                # even started — no log to read yet).
                tail = _death_tail(log_holder)
                # QA (U6c): the preflight text check applies ONLY to
                # TimeoutError — preflight is the only network death with no
                # worker log. U6b's RuntimeError embeds the full Cmd (stage
                # and agent names, file paths, prompt), so a project's own
                # "preflight" stage name must not classify a clean-log crash
                # as network. A crash death is classified by its log tail.
                # Pinned by test_crash_text_with_preflight_word_not_network.
                if _net.is_network_failure(tail) or (
                    isinstance(e, TimeoutError)
                    and _net.is_preflight_timeout(str(e))
                ):
                    net_retries, net_retry_scheduled = _handle_net_death(
                        s_name=s_name, net_retries=net_retries,
                        net_limit=net_limit, net_key=net_key,
                        project_dir=project_dir, logs_dir=logs_dir,
                        outbox=outbox, todo_id=current_todo,
                        prefixes=prefixes,
                    )
                    break  # scheduled → next net round; exhausted → failure path
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
            tail = _death_tail(log_holder)
            if _net.is_network_failure(tail):
                net_retries, net_retry_scheduled = _handle_net_death(
                    s_name=s_name, net_retries=net_retries,
                    net_limit=net_limit, net_key=net_key,
                    project_dir=project_dir, logs_dir=logs_dir,
                    outbox=outbox, todo_id=current_todo,
                    prefixes=prefixes,
                )
                break  # scheduled → next net round; exhausted → failure path

            # dogfood-11: no signal — retry only when the worker left NO work
            # evidence. With work present, salvage lets the supervisor ACK it;
            # a rerun could overwrite or duplicate usable changes.
            worker_has_work = stage_produced_work
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
                from .signals import clean_stage_signals
                clean_stage_signals(outbox, current_todo, *expected_signal_prefixes(s_kind))
                continue

            break

        if signal:
            break
        if net_retry_scheduled:
            continue  # next net round — the endpoint had a chance to recover
        break

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
        # on the first failure of a brand-new stage).
        prev_state = read_state(project_dir) or {}
        try:
            prev_count = int(prev_state.get("salvage_count", 0) or 0)
        except (TypeError, ValueError):
            prev_count = 0
        salvage_key = f"{current_todo}:{s_name}"
        attempt = prev_count + 1 if prev_state.get("salvage_count_key") == salvage_key else 1

        _write_salvage_prompt(
            project_dir, current_todo, s_name, baseline_sha, logs_dir,
            attempt=attempt, auto_retries=attempt_no,
        )
        # B2: the salvage window is over — the prompt is written.
        from . import run_state as _run_state
        _run_state.add_downtime(
            project_dir, _time.monotonic() - salvage_detect,
            reason=f"salvage:{current_todo}:{s_name}", logs_dir=logs_dir,
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
    for consumed_suffix in (".ready", ".md.ready"):
        try:
            (outbox / f"{signal}{consumed_suffix}").unlink()
        except FileNotFoundError:
            pass

    action, target = resolve_transition(stage, sig_type)
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
