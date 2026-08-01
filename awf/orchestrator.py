"""Pipeline execution engine — the core state machine.

A6 refactor: split into focused modules. This file keeps only the state
machine (``run_pipeline``) and a few helpers. Re-exports preserved for
backward compatibility with tests and external callers.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths, todos, verify
from ._env import awf_subprocess_env as _awf_subprocess_env  # noqa: F401
from ._log import log as _log  # noqa: F401
from .agent_stage import (  # noqa: F401
    collect_handoff as _collect_handoff,
)
from .agent_stage import (
    resolve_prev_handoffs as _resolve_prev_handoffs,
)
from .agent_stage import (
    run_agent_stage as _run_agent_stage,
)
from .commit_gate import maybe_commit as _maybe_commit  # noqa: F401
from .pipeline import Stage, load_stages, resolve_pipeline_file
from .plan_progress import (  # noqa: F401
    extract_step_id_from_todo as _extract_step_id_from_todo,
)
from .plan_progress import (
    mark_plan_step_done as _mark_plan_step_done,
)
from .plan_progress import (
    print_progress_report as _print_progress_report,
)
from .signal_watch import run_subprocess_until_signal as _run_subprocess_until_signal  # noqa: F401
from .signals import (
    expected_signal_prefixes,
    read_signal_for_todo,
    signal_type,
    wait_for_signal,
)
from .supervisor import (  # noqa: F401
    build_prompt as _build_prompt,
)
from .supervisor import (
    get_agent_name as _get_agent_name,
)
from .supervisor import (
    get_role_model as _get_role_model,
)
from .supervisor import (
    global_roles_dir as _global_roles_dir,
)
from .supervisor import (
    resolve_role_file as _resolve_role_file,
)
from .supervisor import (
    run_supervisor_stage as _run_supervisor_stage,
)
from .supervisor import (
    run_supervisor_via_subprocess as _run_supervisor_via_subprocess,
)
from .supervisor import (
    wait_for_supervisor_signal as _wait_for_supervisor_signal,
)
from .transitions import resolve_transition

# Public re-exports for back-compat — keep underscore aliases pointing to the
# new module locations so existing tests/external callers don't break.
__all__ = [
    "_awf_subprocess_env",
    "_build_prompt",
    "_collect_handoff",
    "_extract_step_id_from_todo",
    "_find_active_todo",
    "_find_stage_index",
    "_get_agent_name",
    "_get_role_model",
    "_global_roles_dir",
    "_log",
    "_mark_plan_step_done",
    "_maybe_commit",
    "_print_progress_report",
    "_read_baseline_sha",
    "_resolve_prev_handoffs",
    "_resolve_role_file",
    "_run_agent_stage",
    "_run_subprocess_until_signal",
    "_run_supervisor_stage",
    "_run_supervisor_via_subprocess",
    "_wait_for_supervisor_signal",
    "run_pipeline",
]


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
    _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)

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
    _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)
    new_todo = _find_active_todo(project_dir)
    if not new_todo:
        print("Rollback: supervisor did not create a new TODO. Stopping.", file=sys.stderr)
        _log(logs_dir, "Rollback: no new TODO after replan — stopping")
        return -1, current_todo, 1
    return target_idx, new_todo, 0


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

    Extracted from run_pipeline for testability (QA-report T1) and to
    reduce run_pipeline's cognitive complexity (auditor finding).
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


def run_pipeline(args: Any) -> int:
    """Execute the pipeline and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    agentic = paths.agentic_dir(project_dir)

    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    config_file = paths.config_file(project_dir)
    if not config_file.exists():
        print("No .agentic/config.yaml found. Run 'awf init' first.")
        return 1

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    context_dir = paths.context_dir(project_dir)
    logs_dir = paths.agentic_dir(project_dir) / "logs"
    for d in (inbox, outbox, context_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    config = cfg_mod.load(project_dir)

    pipeline_name = getattr(args, "pipeline", None)
    from_stage = getattr(args, "from_stage", None)
    auto = getattr(args, "auto", False)

    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config)
    except FileNotFoundError as e:
        # П5: was a bare error message. Now gives the user a clear next step.
        # Pipeline configuration is created by the project-setup form
        # (agent-workflow-ui plugin). Without it, no pipeline can run.
        print(f"ERROR: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("Pipeline configuration not found.", file=sys.stderr)
        print(file=sys.stderr)
        print("To set up the pipeline + roles, open the project-setup form.", file=sys.stderr)
        print("In opencode, ask your agent to call MCP tool:", file=sys.stderr)
        print('  agent-workflow-ui_open_form(template="project-setup")', file=sys.stderr)
        print(file=sys.stderr)
        print(f"Or create {paths.agentic_dir(project_dir) / 'pipelines' / 'default.yaml'} manually.", file=sys.stderr)
        _log(logs_dir, "Pipeline file not found — printed setup instructions")
        return 1

    stages = load_stages(pipeline_file)
    if not stages:
        print(f"ERROR: No stages found in {pipeline_file}")
        return 1

    total = len(stages)
    print("=== Agentic Workflow: Starting Pipeline ===")
    print(f"Pipeline: {pipeline_file}")
    print(f"Stages: {' '.join(s.name for s in stages)}")
    print()
    _log(logs_dir, f"Pipeline started with {total} stages: {' '.join(s.name for s in stages)}")

    retry_counts = [0] * total

    stage_idx = 0
    if from_stage:
        stage_idx = _find_stage_index(stages, from_stage)
        if stage_idx == -1:
            print(f"ERROR: Stage '{from_stage}' not found in pipeline")
            return 1
        _log(logs_dir, f"Starting from stage: {from_stage} (index {stage_idx})")

    current_todo = ""

    while 0 <= stage_idx < total:
        stage = stages[stage_idx]
        s_name = stage.name
        s_role = stage.role
        s_kind = stage.kind
        s_desc = stage.description

        print()
        print("-" * 43)
        print(f"  Stage {stage_idx + 1}/{total}: {s_name} ({s_role} :: {s_kind})")
        if s_desc:
            print(f"  {s_desc}")
        print("-" * 43)
        _log(logs_dir, f"Stage {stage_idx}: {s_name} ({s_role} :: {s_kind})")

        # --- Supervisor stage ---
        if s_role == "supervisor":
            try:
                sup_signal = _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)
            except RuntimeError as e:
                print(f"ERROR: supervisor stage '{s_name}' crashed. Pipeline stopped.", file=sys.stderr)
                print(f"  Details: {e}", file=sys.stderr)
                print(f"  See {logs_dir / 'orchestrator.log'} for full context.", file=sys.stderr)
                _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
                return 1

            if s_kind == "plan":
                current_todo = _find_active_todo(project_dir)
                if not current_todo:
                    print("No active TODO found. Create one first, then continue.")
                    _log(logs_dir, "No active TODO after supervisor stage")
                    return 1
                print(f"Active TODO: {current_todo}")

                # BD-36: Plan checkpoint — preview TODO before agents start.
                rc = _run_plan_checkpoint_gate(
                    current_todo, project_dir, config, auto, logs_dir,
                )
                if rc != 0:
                    return rc

            if s_kind == "verify":
                # C1 fix: check what supervisor actually decided.
                # REVIEW-{todo_id} = rejection → don't commit, don't close Step,
                # escalate to replan (gives supervisor a chance to refine TODO).
                if sup_signal.startswith("REVIEW-"):
                    print(f"Supervisor REJECTED work on {current_todo} (REVIEW signal).", file=sys.stderr)
                    print(f"  See .agentic/outbox/REVIEW-{current_todo}.md for details.", file=sys.stderr)
                    _log(logs_dir, f"C1: verify rejected via REVIEW-{current_todo} — replanning")
                    # Trigger replan: supervisor creates new refined TODO
                    replan_stage = Stage(name="replan", role="supervisor", kind="replan")
                    try:
                        _run_supervisor_stage(replan_stage, current_todo, auto, project_dir, logs_dir)
                    except RuntimeError as e:
                        print(f"ERROR: replan after REVIEW failed: {e}", file=sys.stderr)
                        return 1
                    new_todo = _find_active_todo(project_dir)
                    if new_todo and new_todo != current_todo:
                        current_todo = new_todo
                    # Pipeline stops — user must read REVIEW, fix issues,
                    # then `awf start` to retry from the new TODO.
                    print("Pipeline stopped: supervisor rejected. Read REVIEW, fix, then 'awf start'.", file=sys.stderr)
                    return 1

                # Approved path: ACK or APPROVE signal
                print(f"Supervisor approved {current_todo} ({sup_signal or 'implicit'}).")
                # A1: pass baseline_sha so _maybe_commit isolates changes
                baseline_sha = _read_baseline_sha(project_dir, current_todo)
                _maybe_commit(
                    s_name, current_todo, stage.on_approved,
                    project_dir, logs_dir, auto=auto, baseline_sha=baseline_sha,
                )
                _mark_plan_step_done(project_dir, current_todo, logs_dir)
                stage_idx += 1
                continue

            stage_idx += 1
            continue

        # --- Agent stage ---
        if not current_todo:
            current_todo = _find_active_todo(project_dir)
            if not current_todo:
                print(f"No active TODO for agent stage '{s_name}'. Run supervisor stage first.")
                return 1

        # П3: auto-create baseline SHA if missing (was manual `awf baseline`).
        # Idempotent — if supervisor or previous run created it, leave alone.
        _ensure_baseline_sha(project_dir, current_todo, logs_dir)

        prev_handoffs = _resolve_prev_handoffs(stages, stage_idx, project_dir, todo_id=current_todo)
        try:
            _run_agent_stage(stage, current_todo, project_dir, config, logs_dir, prev_handoffs=prev_handoffs)
        except RuntimeError as e:
            print(f"ERROR: agent stage '{s_name}' (role={s_role}) crashed. Pipeline stopped.", file=sys.stderr)
            print(f"  Details: {e}", file=sys.stderr)
            print(f"  See {logs_dir / 'orchestrator.log'} for full context.", file=sys.stderr)
            _log(logs_dir, f"Pipeline stopped at stage {s_name}: {e}")
            return 1

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

        if not signal:
            print(
                f"WARNING: No signal after agent stage '{s_name}' (worker ran but didn't signal).",
                file=sys.stderr,
            )
            _log(logs_dir, f"No signal after {s_name} — salvage path")

            if auto:
                baseline_sha = _read_baseline_sha(project_dir, current_todo)
                if baseline_sha and verify.detect_work_evidence(project_dir, baseline_sha):
                    print(
                        f"  Worker left changes vs baseline. TODO {current_todo} left ACTIVE for manual salvage.",
                        file=sys.stderr,
                    )
                    print(
                        f"  Inspect: git diff ; awf status ; then write DONE-{current_todo}.ready or replan.",
                        file=sys.stderr,
                    )
                    _log(logs_dir, f"Auto: work detected; {current_todo} left active for manual salvage")
                else:
                    print("  No worker changes and no signal — treating as failure.", file=sys.stderr)
                    _log(logs_dir, f"Auto: no work + no signal — stop at {s_name}")
                return 1

            salvage_stage = Stage(name="salvage", role="supervisor", kind="verify")
            _run_supervisor_stage(salvage_stage, current_todo, auto=False, project_dir=project_dir, logs_dir=logs_dir)
            signal = read_signal_for_todo(outbox, current_todo, *prefixes)
            if not signal:
                print("No signal after supervisor salvage. Stopping.", file=sys.stderr)
                _log(logs_dir, f"Stopped: salvage produced no signal at {s_name}")
                return 1
            print(f"Salvaged signal: {signal}", file=sys.stderr)
            _log(logs_dir, f"Salvaged signal: {signal} via supervisor")

        sig_type = signal_type(signal)
        print(f"Signal classified as: {sig_type}")

        action, target = resolve_transition(stage, sig_type)
        _log(logs_dir, f"Transition: stage={stage_idx} signal={sig_type} -> action={action} target={target}")

        # A6 refactor: dispatch to handler functions (was 50-line if/elif chain).
        if action in ("next", "commit_and_next", "commit_and_report"):
            stage_idx = _handle_next(
                project_dir, logs_dir, s_name, current_todo, action, auto, retry_counts, stage_idx,
            )

        elif action == "escalate":
            stage_idx, current_todo, exit_code = _handle_escalate(
                project_dir, logs_dir, s_name, current_todo, auto, stage, retry_counts, stage_idx,
            )
            if exit_code != 0:
                return exit_code

        elif action == "rollback":
            stage_idx, current_todo, exit_code = _handle_rollback(
                project_dir, logs_dir, stages, current_todo, auto, target,
            )
            if exit_code != 0:
                return exit_code

        elif action == "stop":
            print("Pipeline stopped by policy.")
            _log(logs_dir, f"Pipeline stopped by policy at stage {s_name}")
            return 0

        else:
            print(f"Unknown transition: {action}")
            return 1

    # All stages completed
    print()
    print("=" * 41)
    print("  Pipeline complete!")
    print("=" * 41)
    print()
    _print_progress_report(project_dir, logs_dir)
    print("Run 'awf start' for the next iteration.")
    _log(logs_dir, "Pipeline complete")
    return 0
