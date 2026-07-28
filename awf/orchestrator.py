"""Pipeline execution engine — the core state machine."""
from __future__ import annotations

import logging
import os
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

APPROVE_TIMEOUT_SECONDS: int = int(os.environ.get("AWF_APPROVE_TIMEOUT_SECONDS", "1800"))
APPROVE_POLL_INTERVAL: int = 2


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

    skills_dir = paths.agentic_dir(project_dir) / "skills"
    local_skill = skills_dir / f"{role}.md"
    if local_skill.exists():
        cmd.insert(cmd.index("--"), "--file")
        cmd.insert(cmd.index("--"), str(local_skill))
        print(f"Local skill: {local_skill}")
        _log(logs_dir, f"Using local skill: {local_skill}")
    else:
        _log(logs_dir, f"No local skill for {role} (using role .md only)")

    subprocess.run(cmd, cwd=project_dir, check=False)

    _log(logs_dir, f"Agent stage finished: {role} ({action}) for {todo_id}")


def _maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
    auto: bool = False,
) -> None:
    """Auto-commit if policy is commit_and_next or commit_and_report.

    In auto mode, blocks waiting for APPROVE-TODO-NNNN.ready signal file
    before committing. This preserves supervisor approval gate.
    """
    if policy not in ("commit_and_next", "commit_and_report"):
        return

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return

    if auto:
        inbox = paths.inbox(project_dir)
        approve_signal = inbox / f"APPROVE-{todo_id}.ready"
        print("Auto-mode: waiting for supervisor approval to commit.", file=sys.stderr)
        print(f"  Create signal: awf approve {todo_id}", file=sys.stderr)
        print(f"  Or manually:  touch {approve_signal}", file=sys.stderr)
        _log(logs_dir, f"Auto-mode: waiting for APPROVE signal for {todo_id}")

        import time
        deadline = time.time() + APPROVE_TIMEOUT_SECONDS
        while not approve_signal.exists():
            if time.time() > deadline:
                print(
                    f"ERROR: APPROVE signal not received within {APPROVE_TIMEOUT_SECONDS}s. "
                    f"Pipeline aborting.",
                    file=sys.stderr,
                )
                _log(logs_dir, f"APPROVE timeout for {todo_id}")
                raise TimeoutError(f"APPROVE signal not received for {todo_id}")
            time.sleep(APPROVE_POLL_INTERVAL)
        _log(logs_dir, f"APPROVE signal received for {todo_id}")

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


def _check_needs_normalize(project_dir: Path) -> tuple[bool, list[dict], Path | None]:
    """Check .agentic/state/needs_normalize.yaml.

    Returns (needed, team_list, state_file_path). Does NOT delete the file.
    """
    import yaml

    state_file = paths.agentic_dir(project_dir) / "state" / "needs_normalize.yaml"
    if not state_file.exists():
        return False, [], None
    try:
        data = yaml.safe_load(state_file.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return False, [], None
    if not isinstance(data, dict) or not data.get("needed"):
        return False, [], None
    team = data.get("team", [])
    return True, team if isinstance(team, list) else [], state_file


def _consume_needs_normalize(state_file: Path | None) -> None:
    """Delete the needs_normalize state file after successful normalize."""
    if state_file and state_file.exists():
        state_file.unlink()


def _needs_normalize(project_dir: Path) -> tuple[bool, list[dict]]:
    """Check and consume .agentic/state/needs_normalize.yaml.

    Returns (needed, team_list). Legacy wrapper for backward compatibility.
    """
    needed, team, _state_file = _check_needs_normalize(project_dir)
    if needed and _state_file:
        _consume_needs_normalize(_state_file)
    return needed, team


def _run_normalize_stage(
    team: list[dict],
    project_dir: Path,
    logs_dir: Path,
    *,
    background: bool = False,
) -> None:
    """Run normalize_skills stage — supervisor (current session) does the work."""
    if background:
        print(
            "ERROR: normalize_skills stage cannot run in --background mode.",
            file=sys.stderr,
        )
        print(
            "Run 'awf normalize' manually first, then 'awf start' (without --background).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    effective_team = team
    if not effective_team:
        roles_dir = paths.agentic_dir(project_dir) / "roles"
        if roles_dir.is_dir():
            for rf in sorted(roles_dir.glob("*.md")):
                effective_team.append({"role": rf.stem, "type": "local"})
        else:
            skills_dir = paths.agentic_dir(project_dir) / "skills"
            if skills_dir.is_dir():
                for sf in sorted(skills_dir.glob("*.md")):
                    effective_team.append({"role": sf.stem, "type": "skill-derived"})

    print()
    print("=" * 51)
    print("  NORMALIZE_SKILLS STAGE")
    print("=" * 51)
    print()
    print("Pipeline detected team selection from form submit.")
    print("Team roles (in pipeline order):")
    for i, member in enumerate(effective_team, 1):
        role = member.get("role", "")
        role_type = member.get("type", "")
        print(f"  {i}. {role} ({role_type})")
    print()
    print("Your job as supervisor:")
    print("  1. Read global skills for each role:")
    print("     ~/.config/opencode/skills/<role-slug>/SKILL.md")
    print("     (use Read tool)")
    print("  2. Build conflict matrix: identify overlaps, contradictions")
    print("     in zones of responsibility across roles.")
    print("  3. For each role, write local skill:")
    print(f"     {paths.agentic_dir(project_dir) / 'skills' / '<role>.md'}")
    print("     Format: frontmatter (derived_from_global: true, global_path,")
    print("     global_sha, normalized_at, pipeline_context) + full copy of")
    print("     global skill + 'Project-specific adaptation' section.")
    print("  4. Heuristics for conflicts:")
    print("     - Priority = pipeline order (first wins).")
    print("     - Output contracts per role type (see supervisor.md).")
    print("     - Unresolved conflicts -> plan.md 'Open questions' section.")
    print("  5. When done, press Enter to continue pipeline.")
    print()
    _log(logs_dir, f"Normalize stage started for team of {len(effective_team)}")
    input()
    _log(logs_dir, "Normalize stage completed (supervisor released)")


def _check_skill_drift(project_dir: Path) -> bool:
    """Check if any local skill has stale global_sha.

    Returns True if any drift detected.
    """
    import hashlib

    import yaml

    skills_dir = paths.agentic_dir(project_dir) / "skills"
    if not skills_dir.is_dir():
        return False

    drifted = []
    for local in sorted(skills_dir.glob("*.md")):
        content = local.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            continue
        end = content.find("\n---\n", 4)
        if end == -1:
            continue
        try:
            fm = yaml.safe_load(content[4:end]) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(fm, dict) or not fm.get("derived_from_global"):
            continue
        global_path = Path(str(fm.get("global_path", ""))).expanduser()
        stored_sha = str(fm.get("global_sha", ""))
        if not global_path.exists():
            continue
        current = hashlib.sha256(global_path.read_bytes()).hexdigest()
        if current != stored_sha:
            drifted.append((local.name, global_path.name))

    if drifted:
        print("WARNING: Skill drift detected:", file=sys.stderr)
        for local_name, global_name in drifted:
            print(f"  {local_name} <- {global_name}", file=sys.stderr)
        print("Run 'awf normalize --check-drift' for details, then 'awf normalize'.",
              file=sys.stderr)
        return True
    return False


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
    background = getattr(args, "background", False)
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

    # BD-10-D: drift detection at start
    if _check_skill_drift(project_dir):
        print("Pipeline will run normalize_skills stage due to drift.", file=sys.stderr)
        state_dir = paths.agentic_dir(project_dir) / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime, timezone
        (state_dir / "needs_normalize.yaml").write_text(
            f"needed: true\ntrigger: drift\nmarked_at: {datetime.now(timezone.utc).isoformat()}\n"
        )

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
                _maybe_commit(s_name, current_todo, stage.on_approved, project_dir, logs_dir, auto=auto)
                stage_idx += 1
                continue

            stage_idx += 1

            # BD-10-C: insert normalize_skills stage after plan
            if s_name == "plan":
                needed, team, state_file = _check_needs_normalize(project_dir)
                if needed:
                    try:
                        _run_normalize_stage(team, project_dir, logs_dir, background=background)
                        _consume_needs_normalize(state_file)
                    except Exception:
                        _log(logs_dir, "normalize_skills failed — state file preserved for retry")
                        raise

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
            _maybe_commit(s_name, current_todo, action, project_dir, logs_dir, auto=auto)
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
