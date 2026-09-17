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

# ─── shared helpers ─────────────────────────────────────────────────────


def _find_stage_index(stages: list[Stage], name: str) -> int:
    """Find stage index by name. Returns -1 if not found."""
    for i, s in enumerate(stages):
        if s.name == name:
            return i
    return -1


def _find_active_todo(project_dir: Path) -> str:
    """Find newest active TODO in inbox."""
    return todos.newest_active(project_dir)


def _read_baseline_sha(project_dir: Path, todo_id: str) -> str:
    """Read baseline SHA from .agentic/context/BASELINE-{todo_id}.sha."""
    if not todo_id:
        return ""
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not sha_file.is_file():
        return ""
    return sha_file.read_text(encoding="utf-8").strip().split("\n")[0]


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


def _handle_next(
    project_dir: Path,
    logs_dir: Path,
    s_name: str,
    current_todo: str,
    action: str,
    auto: bool,
    retry_counts: list[int],
    stage_idx: int,
) -> int:
    """Transition: next / commit_and_next / commit_and_report."""
    baseline_sha = _read_baseline_sha(project_dir, current_todo)
    _maybe_commit(s_name, current_todo, action, project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha)
    print("Moving to next stage.")
    retry_counts[stage_idx] = 0
    return stage_idx + 1


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
    _run_supervisor_stage(
        replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
    )

    new_todo = _find_active_todo(project_dir)
    if not new_todo:
        print("Supervisor did not create a new TODO. Stopping.")
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
) -> tuple[int, str, int]:
    """Transition: rollback to a target stage + supervisor replan.

    Returns (new_stage_idx, new_current_todo, exit_code).
    """
    target_idx = _find_stage_index(stages, target)
    if target_idx < 0:
        print(f"ERROR: Rollback target '{target}' not found in pipeline")
        return -1, current_todo, 1

    print(f"Rolling back to stage: {stages[target_idx].name}")
    _log(logs_dir, f"Rollback to stage {stages[target_idx].name} (index {target_idx})")

    replan_stage = Stage(name="replan", role="supervisor", kind="replan")
    _run_supervisor_stage(
        replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
    )
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
    from .plan_checkpoint import is_checkpoint_enabled, run_plan_checkpoint

    if not is_checkpoint_enabled(config, auto):
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
) -> None:
    """DF5-4: Write a SALVAGE-{todo_id}.md file to inbox.

    This file explains to the supervisor (in opencode) what happened:
    - Worker ran but didn't produce a DONE/BLOCKED signal
    - Git diff stat shows what work was left
    - Supervisor needs to decide: ACK (accept), REVIEW (reject), or replan
    - Dogfood-11: on repeat salvages (attempt >= 2) an escalation block tells
      the supervisor not to retry the same scope, but to split the task /
      require incremental writes / change the stage model instead.
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

    if attempt >= 2:
        parts += [
            f"## ⚠️ ATTEMPT {attempt}: do NOT retry the same scope",
            "",
            f"This stage already ended without a signal {attempt - 1} time(s) in a row.",
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
        # R5: Two-phase plan — Brief → checkpoint → TODO
        inbox_dir = paths.inbox(project_dir)
        brief_signals = sorted(inbox_dir.glob("BRIEF-TODO-*.ready")) if inbox_dir.is_dir() else []

        if brief_signals:
            # R5: Brief detected — run checkpoint on Brief, then write TODO
            brief_name = brief_signals[0].stem  # BRIEF-TODO-0001
            current_todo = brief_name.replace("BRIEF-", "")  # TODO-0001
            print(f"R5: Brief detected for {current_todo}")
            _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir)

            rc = _run_plan_checkpoint_gate(current_todo, project_dir, config, auto, logs_dir)
            if rc != 0:
                return current_todo, 0, rc

            # Phase 2: call supervisor again to write TODO based on approved Brief
            print("R5: Brief approved — supervisor writing TODO for agent...")
            _log(logs_dir, f"R5: Brief approved, requesting TODO for {current_todo}")
            write_todo_stage = Stage(name="write-todo", role="supervisor", kind="plan")
            try:
                _run_supervisor_stage(
                    write_todo_stage, current_todo, auto,
                    project_dir=project_dir, logs_dir=logs_dir,
                    pipeline_name=pipeline_name,
                )
            except (RuntimeError, TimeoutError) as e:
                print(f"ERROR: write-todo stage crashed: {e}", file=sys.stderr)
                return current_todo, 0, 1
        else:
            # Backward compat: no Brief, old flow (supervisor wrote TODO directly)
            rc = _run_plan_checkpoint_gate(current_todo, project_dir, config, auto, logs_dir)
            if rc != 0:
                return current_todo, 0, rc

        current_todo = _find_active_todo(project_dir)
        if not current_todo:
            print("No active TODO found. Create one first, then continue.")
            _log(logs_dir, "No active TODO after supervisor stage")
            return current_todo, 0, 1
        print(f"Active TODO: {current_todo}")
        _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir, phase="brief")
        return current_todo, 1, 0  # next stage

    if s_kind == "verify":
        _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir, phase="verify")
        if not sup_signal:
            print(f"ERROR: verify stage produced no supervisor signal for {current_todo}.", file=sys.stderr)
            _log(logs_dir, "verify: empty supervisor signal — pipeline aborted")
            return current_todo, 0, 1

        if sup_signal.startswith("REVIEW-"):
            print(f"Supervisor REJECTED work on {current_todo} (REVIEW signal).", file=sys.stderr)
            _log(logs_dir, f"C1: verify rejected via REVIEW-{current_todo} — replanning")
            replan_stage = Stage(name="replan", role="supervisor", kind="replan")
            try:
                _run_supervisor_stage(
                    replan_stage, current_todo, auto, project_dir, logs_dir, pipeline_name
                )
            except (RuntimeError, TimeoutError) as e:
                print(f"ERROR: replan after REVIEW failed: {e}", file=sys.stderr)
                return current_todo, 0, 1
            new_todo = _find_active_todo(project_dir)
            if new_todo and new_todo != current_todo:
                current_todo = new_todo
                # Persist replanned TODO so continue resumes the right task.
                _write_state(project_dir, todo_id=current_todo, logs_dir=logs_dir)
            print("Pipeline stopped: supervisor rejected.", file=sys.stderr)
            return current_todo, 0, 1

        # Approved path: ACK or APPROVE signal
        print(f"Supervisor approved {current_todo} ({sup_signal or 'implicit'}).")
        baseline_sha = _read_baseline_sha(project_dir, current_todo)
        commit_ok = _maybe_commit(
            s_name, current_todo, stage.on_approved,
            project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha,
        )
        if not commit_ok:
            print(
                f"Commit failed for {current_todo} — TODO NOT archived, "
                f"changes left in working tree for manual review.",
                file=sys.stderr,
            )
            _log(logs_dir, f"Commit failed at verify for {current_todo} — not archived")
            return current_todo, 0, 1
        _mark_plan_step_done(project_dir, current_todo, logs_dir)
        from .todos import archive_todo
        archived = archive_todo(project_dir, current_todo)
        if archived:
            _log(logs_dir, f"DF6-1: archived {current_todo} → {archived}")
        return current_todo, 1, 0  # next stage

    # Other supervisor kinds (e.g. replan without verify)
    return current_todo, 1, 0


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

    # F4: auto-retry transient failures (vllm cold-start, empty output <30s)
    MAX_TRANSIENT_RETRIES = 2
    TRANSIENT_THRESHOLD_SEC = 30

    signal = None
    for transient_retry in range(MAX_TRANSIENT_RETRIES + 1):
        import time as _time
        agent_start = _time.monotonic()
        try:
            _run_agent_stage(stage, current_todo, project_dir, config, logs_dir,
                             prev_handoffs=prev_handoffs, hard_timeout=agent_hard_timeout)
        except (RuntimeError, TimeoutError) as e:
            print(f"ERROR: agent stage '{s_name}' crashed. Pipeline stopped.", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
            return current_todo, stage_idx, 1
        agent_elapsed = _time.monotonic() - agent_start

        prefixes = expected_signal_prefixes(s_kind)
        signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if not signal:
            try:
                signal = wait_for_signal(outbox, current_todo, *prefixes, timeout=30)
            except TimeoutError:
                pass

        if not signal:
            baseline_sha = _read_baseline_sha(project_dir, current_todo)
            if baseline_sha and verify.attempt_auto_done(project_dir, current_todo, config, baseline_sha):
                signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if signal:
            break

        # F4: transient failure — worker exited too fast with no signal
        if transient_retry < MAX_TRANSIENT_RETRIES and agent_elapsed < TRANSIENT_THRESHOLD_SEC:
            print(
                f"Worker exited in {agent_elapsed:.0f}s with no signal — "
                f"transient failure? Retrying ({transient_retry + 1}/{MAX_TRANSIENT_RETRIES})...",
                file=sys.stderr,
            )
            _log(logs_dir, f"F4: auto-retry {transient_retry + 1}/{MAX_TRANSIENT_RETRIES} "
                f"for {s_name} (elapsed={agent_elapsed:.0f}s, no signal)")
            from .signals import clean_stage_signals
            clean_stage_signals(outbox, current_todo, *expected_signal_prefixes(s_kind))
            continue

        break

    if not signal:
        # Salvage path
        baseline_sha = _read_baseline_sha(project_dir, current_todo)
        print(f"WARNING: No signal after agent stage '{s_name}'.", file=sys.stderr)
        _log(logs_dir, f"No signal after {s_name} — salvage path")

        # Dogfood-11: count consecutive silent exits for this stage. A repeat
        # salvage means retrying the same scope won't help — the SALVAGE note
        # escalates to task splitting / incremental writes instead.
        prev_state = read_state(project_dir) or {}
        try:
            attempt = int(prev_state.get("salvage_count", 0) or 0) + 1
        except (TypeError, ValueError):
            attempt = 1

        _write_salvage_prompt(
            project_dir, current_todo, s_name, baseline_sha, logs_dir, attempt=attempt,
        )
        _ws(
            project_dir,
            salvage_needed=True, salvage_stage=s_name, salvage_count=attempt,
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
        _run_supervisor_stage(
            salvage_stage, current_todo, auto=False,
            project_dir=project_dir, logs_dir=logs_dir, pipeline_name=pipeline_name
        )
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
    _write_state(project_dir, salvage_count=0, logs_dir=logs_dir)

    action, target = resolve_transition(stage, sig_type)
    _log(logs_dir, f"Transition: stage={stage_idx} signal={sig_type} -> action={action} target={target}")

    if action in ("next", "commit_and_next", "commit_and_report"):
        new_idx = _handle_next(
            project_dir, logs_dir, s_name, current_todo, action, auto, retry_counts, stage_idx,
        )
        return current_todo, new_idx, 0

    elif action == "escalate":
        new_idx, new_todo, exit_code = _handle_escalate(
            project_dir, logs_dir, s_name, current_todo, auto, stage, retry_counts, stage_idx,
            pipeline_name,
        )
        return new_todo, new_idx, exit_code

    elif action == "rollback":
        new_idx, new_todo, exit_code = _handle_rollback(
            project_dir, logs_dir, stages, current_todo, auto, target, pipeline_name,
        )
        return new_todo, new_idx, exit_code

    elif action == "stop":
        print("Pipeline stopped by policy.")
        _log(logs_dir, f"Pipeline stopped by policy at stage {s_name}")
        return current_todo, stage_idx, 0

    else:
        print(f"Unknown transition: {action}")
        return current_todo, stage_idx, 1
