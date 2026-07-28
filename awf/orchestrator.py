"""Pipeline execution engine — the core state machine."""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import git_utils, paths, todos, verify
from .pipeline import Stage, load_stages, resolve_pipeline_file
from .signals import (
    clean_stage_signals,
    expected_signal_prefixes,
    read_signal_for_todo,
    signal_type,
    wait_for_signal,
)
from .transitions import resolve_transition


def _log(logs_dir: Path, message: str) -> None:
    """Append a timestamped line to orchestrator.log."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    log_file = logs_dir / "orchestrator.log"
    with log_file.open("a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")


def _get_agent_name(config: dict, role: str) -> str:
    """Get the agent_name override for a role, defaulting to the role itself."""
    return cfg_mod.get(config, f"models.{role}.agent_name", role) or role


def _build_prompt(action: str, todo_id: str) -> str:
    """Build the prompt string for an agent stage, matching bash run_agent_stage."""
    prompts = {
        "execute_todo": (
            f"Execute all Tasks in {todo_id} via edit tool. Run Verify after each task. "
            f"Run regression verify before any commit. "
            f"Write .agentic/outbox/DONE-{todo_id}.md or BLOCKED-{todo_id}.md and create "
            f"matching .ready signal. Do not commit unless the TODO explicitly includes a "
            f"final Git commit step."
        ),
        "review_code": (
            f"Review the code changes for {todo_id}. Compare TODO with actual git diff. "
            f"Check quality and correctness. Write .agentic/outbox/REVIEW-APPROVED-{todo_id}.md "
            f"or REVIEW-REJECTED-{todo_id}.md with .ready signal."
        ),
        "run_tests": (
            f"Run the full test suite and verification commands. Compare results with baseline. "
            f"Write .agentic/outbox/TEST-PASSED-{todo_id}.md or TEST-FAILED-{todo_id}.md "
            f"with .ready signal."
        ),
        "audit_code": (
            f"Audit the code changes for {todo_id} for security issues. "
            f"Write .agentic/outbox/REVIEW-APPROVED-{todo_id}.md or "
            f"REVIEW-REJECTED-{todo_id}.md with .ready signal."
        ),
    }
    return prompts.get(
        action,
        f"Execute action '{action}' for {todo_id} following role instructions. "
        f"Write result to .agentic/outbox/ with .ready signal.",
    )


log = logging.getLogger(__name__)


def _global_roles_dir() -> Path:
    """Return the global roles directory: ~/.config/awf/roles/."""
    return Path.home() / ".config" / "awf" / "roles"


def _resolve_role_file(role: str, project_dir: Path) -> Path:
    """Resolve role file with project-local → global fallback.

    Returns the existing ``<role>.md`` path or raises ``RuntimeError``
    listing both checked locations.
    """
    project_role = paths.agentic_dir(project_dir) / "roles" / f"{role}.md"
    global_role = _global_roles_dir() / f"{role}.md"

    if project_role.exists():
        return project_role

    if global_role.exists():
        log.info("Role '%s' not in project, falling back to global: %s", role, global_role)
        return global_role

    raise RuntimeError(
        f"Role file '{role}' not found.\n"
        f"  Checked: {project_role}\n"
        f"  Checked: {global_role}"
    )


def _run_supervisor_stage(
    stage: Stage,
    todo_id: str,
    auto: bool,
    project_dir: Path,
    logs_dir: Path,
) -> None:
    """Handle a supervisor stage. In auto mode, skip interactively."""
    action = stage.action
    config = cfg_mod.load(project_dir)
    phases_file = cfg_mod.get(config, "phases.current", ".agentic/phases/plan.md")

    print()
    print("=" * 41)
    print(f"  SUPERVISOR STAGE: {action}")
    print("=" * 41)
    print()
    print("Instructions: .agentic/roles/supervisor.md")
    print(f"Phases file: {phases_file}")
    print()

    if action == "create_todo":
        print("What to do:")
        print("  1. Study the project state and phases file")
        print("  2. Determine the next step")
        print("  3. Create baseline: awf baseline TODO-{NNNN}")
        print("  4. Write task to .agentic/inbox/TODO-{NNNN}.md")
        print("  5. Create signal: .agentic/inbox/TODO-{NNNN}.ready")
    elif action in ("verify_result", "final_verify"):
        print("What to do:")
        print("  1. Read report from .agentic/outbox/")
        print("  2. Run verification commands independently")
        print("  3. Check git diff — changes must be in source files")
        print("  4. Decide: continue / fix / rollback")
        print("  5. If approved: create .agentic/inbox/ACK-{NNNN}.ready")
    elif action == "replan":
        print("Worker returned BLOCKED. Resolve the issue:")
        print("  1. Read .agentic/outbox/BLOCKED-*.md")
        print("  2. Analyze the problem")
        print("  3. Create a new TODO with refined instructions")
        print("  4. Create .agentic/inbox/TODO-{NNNN}.ready")
    elif action == "salvage":
        context_dir = paths.context_dir(project_dir)
        base_sha = ""
        if todo_id:
            sha_file = context_dir / f"BASELINE-{todo_id}.sha"
            if sha_file.exists():
                base_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]
        outbox = paths.outbox(project_dir)
        print("Worker finished but wrote NO signal (timeout/crash). Work may be complete.")
        print()
        print("Investigate the orphaned work:")
        print(f"  1. See what changed:  git diff {base_sha} --stat" if base_sha else "  1. See what changed:  git diff --stat")
        print(f"  2. Worker progress:   cat {outbox}/PROGRESS-{todo_id}.md")
        print("  3. Verify changes independently (build, tests, review).")
        print()
        print("Decide and act:")
        print(f"  - good  -> salvage: write {outbox}/DONE-{todo_id}.md + {outbox}/DONE-{todo_id}.ready")
        print("  - bad   -> rollback to baseline, then create a new TODO (replan)")
        print("  - stuck -> leave as-is and stop")
        print()
        print("After writing a signal (or a new TODO), press Enter to continue.")

    print()
    if auto:
        print("[auto mode] Skipping supervisor wait.")
        _log(logs_dir, f"Supervisor stage {action} auto-skipped")
    else:
        print("When done, press Enter to continue...")
        input()
        _log(logs_dir, f"Supervisor stage {action} completed by user")


def _run_agent_stage(
    stage: Stage,
    todo_id: str,
    project_dir: Path,
    config: dict,
    logs_dir: Path,
) -> None:
    """Spawn opencode run for an agent stage."""
    role = stage.role
    action = stage.action
    agent_name = _get_agent_name(config, role)

    print()
    print("=" * 41)
    print(f"  AGENT STAGE: {role} ({action})")
    print(f"  Task: {todo_id}")
    print("=" * 41)
    print()

    role_file = _resolve_role_file(role, project_dir)
    inbox = paths.inbox(project_dir)
    todo_file = inbox / f"{todo_id}.md"

    prompt = _build_prompt(action, todo_id)

    print(f"Running agent: {agent_name}")
    print(f"Role file: {role_file}")
    print(f"Task: {todo_file}")
    print()

    # Clean stale signals this action could produce
    prefixes = expected_signal_prefixes(action)
    outbox = paths.outbox(project_dir)
    clean_stage_signals(outbox, todo_id, *prefixes)

    _log(logs_dir, f"Agent stage started: {role} ({action}) for {todo_id}")

    cmd = [
        "opencode", "run", "--auto",
        "--agent", agent_name,
        "--file", str(role_file),
        "--file", str(todo_file),
        "--", prompt,
    ]
    subprocess.run(cmd, cwd=project_dir, check=False)

    _log(logs_dir, f"Agent stage finished: {role} ({action}) for {todo_id}")


def _maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
) -> None:
    """Auto-commit if policy is commit_and_next or commit_and_report."""
    if policy not in ("commit_and_next", "commit_and_report"):
        return

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return

    if git_utils.commit_all(project_dir, f"awf({stage_name}): {todo_id}"):
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        ).stdout.strip()
        print(f"Auto-committed: {todo_id} at '{stage_name}' ({sha}).", file=sys.stderr)
        print("Remember to push: git push origin HEAD", file=sys.stderr)
        _log(logs_dir, f"Auto-committed {todo_id} at {stage_name} ({sha})")
    else:
        print(f"No changes to auto-commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"Nothing to auto-commit at {stage_name}")


def _find_stage_index(stages: list[Stage], name: str) -> int:
    """Find stage index by name. Returns -1 if not found."""
    for i, s in enumerate(stages):
        if s.name == name:
            return i
    return -1


def _find_active_todo(project_dir: Path) -> str:
    """Find the newest active TODO."""
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    active = todos.list_active_todos(inbox, outbox)
    return active[0] if active else ""


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
    # --timeout is accepted by argparse for backward compatibility but not yet
    # wired into wait_for_signal. Tracked separately in awf-core backlog.
    _timeout = getattr(args, "timeout", 3600)
    del _timeout

    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
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

    # Retry counts — one per stage
    retry_counts = [0] * total

    # Determine starting stage
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
        s_action = stage.action
        s_desc = stage.description

        print()
        print("-" * 43)
        print(f"  Stage {stage_idx + 1}/{total}: {s_name} ({s_role} :: {s_action})")
        if s_desc:
            print(f"  {s_desc}")
        print("-" * 43)
        _log(logs_dir, f"Stage {stage_idx}: {s_name} ({s_role} :: {s_action})")

        # --- Supervisor stage ---
        if s_role == "supervisor":
            _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)

            if s_action in ("create_todo", "replan"):
                current_todo = _find_active_todo(project_dir)
                if not current_todo:
                    print("No active TODO found. Create one first, then continue.")
                    _log(logs_dir, "No active TODO after supervisor stage")
                    return 1
                print(f"Active TODO: {current_todo}")

            if s_action in ("verify_result", "final_verify"):
                print("Supervisor verification complete.")
                _maybe_commit(s_name, current_todo, stage.on_approved, project_dir, logs_dir)
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

        _run_agent_stage(stage, current_todo, project_dir, config, logs_dir)

        prefixes = expected_signal_prefixes(s_action)

        # Read signal (opencode run is blocking; signal should already exist)
        signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if not signal:
            # Brief fallback poll
            try:
                signal = wait_for_signal(outbox, current_todo, *prefixes, timeout=30)
            except TimeoutError:
                pass

        if not signal:
            # No signal: try auto-DONE
            baseline_sha = ""
            if current_todo:
                sha_file = context_dir / f"BASELINE-{current_todo}.sha"
                if sha_file.exists():
                    baseline_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]

            if baseline_sha and verify.attempt_auto_done(project_dir, current_todo, config, baseline_sha):
                signal = read_signal_for_todo(outbox, current_todo, *prefixes)

        if not signal:
            # Still no signal — escalate
            print(
                f"WARNING: No signal after agent stage '{s_name}' "
                f"(worker ran but didn't signal).",
                file=sys.stderr,
            )
            _log(logs_dir, f"No signal after {s_name} — salvage path")

            if auto:
                baseline_sha = ""
                if current_todo:
                    sha_file = context_dir / f"BASELINE-{current_todo}.sha"
                    if sha_file.exists():
                        baseline_sha = sha_file.read_text(encoding="utf-8").strip().split("\n")[0]

                if baseline_sha and verify.detect_work_evidence(project_dir, baseline_sha):
                    print(
                        f"  Worker left changes vs baseline. TODO {current_todo} left ACTIVE "
                        f"for manual salvage.",
                        file=sys.stderr,
                    )
                    print(
                        f"  Inspect: git diff ; awf status ; then write "
                        f"DONE-{current_todo}.ready or replan.",
                        file=sys.stderr,
                    )
                    _log(logs_dir, f"Auto: work detected; {current_todo} left active for manual salvage")
                else:
                    print("  No worker changes and no signal — treating as failure.", file=sys.stderr)
                    _log(logs_dir, f"Auto: no work + no signal — stop at {s_name}")
                return 1

            # Interactive salvage
            _run_supervisor_stage(stage, current_todo, auto=False, project_dir=project_dir, logs_dir=logs_dir)
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

        if action in ("next", "commit_and_next", "commit_and_report"):
            _maybe_commit(s_name, current_todo, action, project_dir, logs_dir)
            print("Moving to next stage.")
            retry_counts[stage_idx] = 0
            stage_idx += 1

        elif action == "escalate":
            max_r = stage.max_retries
            if retry_counts[stage_idx] < max_r:
                retry_counts[stage_idx] += 1
                print(f"BLOCKED — escalating to supervisor (attempt {retry_counts[stage_idx]}/{max_r})")
                _log(logs_dir, "Escalating to supervisor for retry")

                _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)

                new_todo = _find_active_todo(project_dir)
                if new_todo:
                    current_todo = new_todo
                    print(f"New TODO: {current_todo} — retrying stage '{s_name}'")
                else:
                    print("Supervisor did not create a new TODO. Stopping.")
                    return 1
            else:
                print(f"BLOCKED — max retries reached ({max_r}). Pipeline stopped.")
                _log(logs_dir, f"Max retries reached for stage {s_name}")
                return 1

        elif action == "rollback":
            target_idx = _find_stage_index(stages, target)
            if target_idx >= 0:
                print(f"Rolling back to stage: {stages[target_idx].name}")
                _log(logs_dir, f"Rollback to stage {stages[target_idx].name} (index {target_idx})")
                stage_idx = target_idx

                _run_supervisor_stage(stage, current_todo, auto, project_dir, logs_dir)
                new_todo = _find_active_todo(project_dir)
                if new_todo:
                    current_todo = new_todo
            else:
                print(f"ERROR: Rollback target '{target}' not found in pipeline")
                return 1

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
    print("Run 'awf status' to check state.")
    print("Run 'awf report' for summary.")
    print("Run 'awf start' for the next iteration.")
    _log(logs_dir, "Pipeline complete")
    return 0
