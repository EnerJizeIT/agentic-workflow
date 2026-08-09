"""DAUD-7: Pipeline engine — extracted stage handlers.

Separates "execute a stage" (side effects) from "decide what to do next"
(transition logic). This makes run_pipeline() a thin dispatch loop and
makes stage handlers independently testable.

Architecture (DeepSeek audit recommendation):
- run_pipeline(): setup + loop + dispatch (~80 lines)
- execute_supervisor_stage(): plan/verify/salvage branches
- execute_agent_stage(): subprocess + signal + transition dispatch

No event bus, no plugin registry, no strategy pattern.
State machine on if/elif with dataclass — sufficient.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import paths, verify
from ._log import log as _log
from .agent_stage import resolve_prev_handoffs as _resolve_prev_handoffs
from .agent_stage import run_agent_stage as _run_agent_stage
from .commit_gate import maybe_commit as _maybe_commit
from .orchestrator import (
    _ensure_baseline_sha,
    _find_active_todo,
    _handle_escalate,
    _handle_next,
    _handle_rollback,
    _read_baseline_sha,
    _run_plan_checkpoint_gate,
    _write_salvage_prompt,
)
from .pipeline import Stage
from .pipeline_state import write_state as _write_state
from .plan_progress import mark_plan_step_done as _mark_plan_step_done
from .signals import expected_signal_prefixes, read_signal_for_todo, signal_type, wait_for_signal
from .supervisor import run_supervisor_stage as _run_supervisor_stage
from .transitions import resolve_transition


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
        _maybe_commit(
            s_name, current_todo, stage.on_approved,
            project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha,
        )
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

        _write_salvage_prompt(project_dir, current_todo, s_name, baseline_sha, logs_dir)
        _ws(project_dir, salvage_needed=True, salvage_stage=s_name, logs_dir=logs_dir)

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
