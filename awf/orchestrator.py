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

    # AUD14-05: lazy import — cross-imports between core modules stay
    # function-local to avoid import cycles (see .api.* imports below).
    from .api._errors import AwfApiError

    pipeline_name = getattr(args, "pipeline", None)
    from_stage = getattr(args, "from_stage", None)
    auto = getattr(args, "auto", False)
    # KAUD-4: read --timeout from CLI args, pass to agent stages
    cli_timeout = getattr(args, "timeout", None)
    try:
        agent_hard_timeout = int(cli_timeout) if cli_timeout else None
    except (ValueError, TypeError):
        print(f"WARNING: invalid timeout '{cli_timeout}' — ignoring.", file=sys.stderr)
        agent_hard_timeout = None
    try:
        pipeline_file = resolve_pipeline_file(project_dir, pipeline_name, config)
    except AwfApiError as e:
        # AUD14-05: invalid --pipeline name (path traversal attempt) —
        # fail cleanly instead of loading an external YAML.
        print(f"ERROR: {e}", file=sys.stderr)
        _log(logs_dir, f"Pipeline file rejected: {e}")
        return 1
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

    # Start dashboard HTTP server (live updates, no file:// reload)
    dashboard_port = 0
    _dashboard_server = None
    try:
        from .api.dashboard_server import start_dashboard_server

        # Day-3 (dashboard review): reuse the previous port so an already-open
        # browser tab survives restarts (retry_stage/continue/kill+continue).
        # If the old port is still busy, fall back to a random one.
        port_file = paths.agentic_dir(project_dir) / "state" / "dashboard_port"
        prev_port = 0
        try:
            if port_file.is_file():
                prev_port = int(port_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            prev_port = 0
        try:
            dashboard_port, _dashboard_server = start_dashboard_server(
                project_dir, port=prev_port
            )
        except OSError:
            dashboard_port, _dashboard_server = start_dashboard_server(project_dir)
        _log(logs_dir, f"Dashboard server: http://127.0.0.1:{dashboard_port}")
        # Write port to separate file (survives state overwrites in stage loop)
        port_file.parent.mkdir(parents=True, exist_ok=True)
        port_file.write_text(str(dashboard_port), encoding="utf-8")
    except Exception as e:
        _log(logs_dir, f"Dashboard server failed to start: {e}")

    stage_idx = 0
    if from_stage:
        stage_idx = _find_stage_index(stages, from_stage)
        if stage_idx == -1:
            print(f"ERROR: Stage '{from_stage}' not found in pipeline")
            return 1
        _log(logs_dir, f"Starting from stage: {from_stage} (index {stage_idx})")

    # NEG-2026-09-19 R1: a pinned TODO (run queue item) wins over the
    # "newest active" heuristic — queue order must be honored.
    current_todo = str(getattr(args, "todo_id", "") or "")

    # KA2-6: forward the CLI timeout to supervisor stages via env var
    # (covers primary, escalate, rollback, salvage — without threading it
    # through 5+ function signatures). AUD04-10: set at the loop boundary
    # and restore on EVERY exit path — early return, exception, clean exit.
    _prev_timeout = os.environ.get("AWF_SUPERVISOR_TIMEOUT")
    if agent_hard_timeout:
        os.environ["AWF_SUPERVISOR_TIMEOUT"] = str(agent_hard_timeout)

    try:
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
                # dogfood-11: a new stage start means any previous salvage was
                # resolved (ACK/retry) — clear the flag, or the dashboard would
                # keep showing "Salvage" forever (write_state merges fields).
                salvage_needed=False, salvage_stage=None,
                # AUD02-01: same for the previous stage's signal — a stale
                # BLOCKED would re-trigger the blocked wake-up during a retried
                # stage before the worker emits its own signal.
                last_signal=None,
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
    finally:
        # P2/AUD04-10: restore env var (don't leak AWF_SUPERVISOR_TIMEOUT
        # to the caller — in-process foreground runs, MCP continue, tests).
        if agent_hard_timeout:
            if _prev_timeout is not None:
                os.environ["AWF_SUPERVISOR_TIMEOUT"] = _prev_timeout
            else:
                os.environ.pop("AWF_SUPERVISOR_TIMEOUT", None)
