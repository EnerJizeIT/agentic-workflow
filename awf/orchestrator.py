"""Pipeline execution engine — the core state machine.

A6 refactor: split into focused modules. This file keeps only the state
machine (``run_pipeline``) and a few helpers. Re-exports preserved for
backward compatibility with tests and external callers.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
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
from .pipeline_state import clear_state, write_state
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

# T1.5: re-exports for white-box tests (from awf.orchestrator import _foo).
# __all__ removed — ruff per-file-ignore (pyproject.toml) protects the
# underscore aliases from being auto-removed by ruff --fix. Tests import
# explicitly; no star-import callers.


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
    # KAUD-4: read --timeout from CLI args, pass to agent stages
    cli_timeout = getattr(args, "timeout", None)
    agent_hard_timeout = int(cli_timeout) if cli_timeout else None
    # KA2-6: also forward to supervisor stages via env var (covers all
    # supervisor calls: primary, escalate, rollback, salvage — without
    # threading timeout through 5+ function signatures).
    if agent_hard_timeout:
        os.environ["AWF_SUPERVISOR_TIMEOUT"] = str(agent_hard_timeout)

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
        write_state(
            project_dir, logs_dir=logs_dir, stage_idx=stage_idx,
            stage_name=s_name, stage_kind=s_kind, stage_role=s_role,
            todo_id=current_todo, pipeline_pid=os.getpid(),
        )
        from .api.dashboard import generate_dashboard
        generate_dashboard(project_dir)

        # DAUD-7: dispatch to extracted stage handlers
        from .pipeline_engine import execute_agent_stage, execute_supervisor_stage

        if s_role == "supervisor":
            current_todo, delta, rc = execute_supervisor_stage(
                stage, current_todo, auto, project_dir, config, logs_dir, pipeline_name
            )
            if rc != 0:
                return rc
            stage_idx += delta
        else:
            current_todo, stage_idx, rc = execute_agent_stage(
                stage, current_todo, project_dir, config, logs_dir,
                stages, stage_idx, retry_counts, auto, agent_hard_timeout,
            )
            if rc != 0:
                return rc

    # All stages completed
    print()
    print("=" * 41)
    print("  Pipeline complete!")
    print("=" * 41)
    print()
    _print_progress_report(project_dir, logs_dir)
    print("Run 'awf start' for the next iteration.")
    _log(logs_dir, "Pipeline complete")
    # T4.1: clear structured state on clean exit (pipeline not running anymore)
    # But first generate final dashboard so user sees "complete" state
    from .api.dashboard import generate_dashboard as _gen_dash
    _gen_dash(project_dir)
    clear_state(project_dir, logs_dir=logs_dir)
    return 0


def _write_salvage_prompt(
    project_dir: Path,
    todo_id: str,
    stage_name: str,
    baseline_sha: str | None,
    logs_dir: Path,
) -> None:
    """DF5-4: Write a SALVAGE-{todo_id}.md file to inbox.

    This file explains to the supervisor (in opencode) what happened:
    - Worker ran but didn't produce a DONE/BLOCKED signal
    - Git diff stat shows what work was left
    - Supervisor needs to decide: ACK (accept), REVIEW (reject), or replan
    """
    from ._atomic import atomic_write_text

    inbox = paths.inbox(project_dir)
    salvage_file = inbox / f"SALVAGE-{todo_id}.md"

    parts: list[str] = [
        f"# Salvage needed: {stage_name} did not signal",
        "",
        f"**TODO:** {todo_id}",
        f"**Stage:** {stage_name}",
        f"**Time:** {datetime.now(timezone.utc).isoformat()}",
        "",
        "## What happened",
        "",
        f"The worker at stage `{stage_name}` completed its subprocess (exit 0) but",
        f"did NOT create the DONE-{todo_id}.ready signal file in outbox.",
        "This usually means the worker finished its task but forgot the",
        "completion signal (common with smaller models / turn-budget limits).",
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
