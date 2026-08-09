"""Pipeline execution entry point — thin dispatch loop.

All stage handlers and transition logic live in ``pipeline_engine``.
This module is responsible for:
- Setup (dirs, config, pipeline file)
- Stage loop (write state → generate dashboard → dispatch to engine)
- Completion (progress report, state cleanup)

No circular dependency: orchestrator imports from pipeline_engine (one direction).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from ._log import log as _log
from .pipeline import load_stages, resolve_pipeline_file
from .pipeline_engine import (
    _find_stage_index,
    execute_agent_stage,
    execute_supervisor_stage,
)
from .pipeline_state import clear_state, read_state, write_state
from .plan_progress import (
    print_progress_report as _print_progress_report,
)


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
    # SMO: write phase=done + preserve goal for next iteration
    prev_state = read_state(project_dir) or {}
    clear_state(project_dir, logs_dir=logs_dir)
    write_state(project_dir, phase="done", goal=prev_state.get("goal"))
    return 0
