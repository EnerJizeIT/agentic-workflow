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
import re
import sys
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from ._atomic import atomic_write_text
from ._log import log as _log
from .pipeline import load_stages, pipeline_snapshot_text, resolve_pipeline_file
from .pipeline_engine import (
    _find_stage_index,
    execute_agent_stage,
    execute_supervisor_stage,
)
from .pipeline_state import clear_state, read_state, write_state
from .plan_progress import (
    print_progress_report as _print_progress_report,
)


def _pipeline_snapshot_file(project_dir: Path, todo_id: str) -> Path:
    """ORCH M3.3: the per-TODO pipeline snapshot path in the context dir."""
    return paths.context_dir(project_dir) / f"PIPELINE-{todo_id}.yaml"


def _write_pipeline_snapshot(
    project_dir: Path,
    stages: list,
    pipeline_file: Path,
    todo_id: str,
    logs_dir: Path,
) -> None:
    """ORCH M3.3: capture the resolved stages as PIPELINE-{todo_id}.yaml.

    Atomic write (temp + rename, same as every context artifact). Best
    effort by design: the snapshot is a stability feature, not a gate —
    a failed capture must not kill the launch (the unit keeps running on
    the live stages; the next launch retries the capture).
    """
    try:
        text = pipeline_snapshot_text(stages, pipeline_file.stem, todo_id)
        atomic_write_text(_pipeline_snapshot_file(project_dir, todo_id), text)
        _log(
            logs_dir,
            f"Pipeline snapshot captured: PIPELINE-{todo_id}.yaml "
            f"(source {pipeline_file.name})",
        )
    except Exception as e:
        _log(logs_dir, f"WARNING: pipeline snapshot capture failed: {e}")


def _apply_pipeline_snapshot(
    project_dir: Path,
    stages: list,
    pipeline_file: Path,
    todo_id: str,
    resuming: bool,
    logs_dir: Path,
) -> list:
    """ORCH M3.3: read the unit's pipeline snapshot (or capture it).

    Snapshot present → the run resumes the definition it started with,
    whatever the published pipeline file says now (contract: changing the
    published template does not change the snapshot of a running stage).
    Snapshot absent → degrade, not refuse: the live pipeline is used and
    captured so the next resume is stable; a resuming launch (from_stage
    set) without a snapshot is an old unit or a manual launch and gets a
    warning. A snapshot that fails to load degrades the same way — a unit
    must not die on its own shadow file.
    """
    if not re.fullmatch(r"TODO-\d{4,}", todo_id or ""):
        return stages
    # AUD14-05: function-local — awf._errors is a zero-import leaf (the
    # awf.api re-export would pull the package init in).
    from ._errors import AwfApiError

    snap = _pipeline_snapshot_file(project_dir, todo_id)
    if snap.is_file():
        try:
            snap_stages = load_stages(snap)
        except AwfApiError as e:
            # F-01 (QA review, M3.3): a structurally invalid snapshot
            # (valid YAML, bad stage form / unknown key / bad id) makes
            # load_stages raise instead of returning [] — degrade it the
            # same way as byte corruption: the unit must not die on its
            # own shadow file.
            print(
                f"ERROR: pipeline snapshot {snap.name} is invalid: {e}",
                file=sys.stderr,
            )
            snap_stages = []
        if snap_stages:
            _log(logs_dir, f"Pipeline {todo_id} resumed from snapshot {snap.name}")
            return snap_stages
        print(
            f"WARNING: pipeline snapshot {snap.name} is unreadable — "
            "falling back to the live pipeline.",
            file=sys.stderr,
        )
        _log(logs_dir, f"Snapshot {snap.name} unreadable — live pipeline fallback")
    elif resuming:
        print(
            f"WARNING: no pipeline snapshot for {todo_id} (old unit or "
            "manual launch) — using the live pipeline.",
            file=sys.stderr,
        )
        _log(
            logs_dir,
            f"No snapshot for {todo_id} (old unit or manual launch) — "
            "live pipeline fallback",
        )
    _write_pipeline_snapshot(project_dir, stages, pipeline_file, todo_id, logs_dir)
    return stages


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

    # ORCH M3.3: the unit runs on the pipeline definition it started with.
    # Every launch path (awf_start / awf_run_next / awf_continue, foreground
    # and the background child) funnels into run_pipeline, so this single
    # point covers all of them: a pinned TODO resumes its snapshot
    # PIPELINE-{todo_id}.yaml — a republished pipelines/*.yaml does not
    # change the resumed unit. An unpinned start has no TODO yet; its
    # snapshot is captured in the stage loop once the plan stage resolves
    # the unit.
    stages = _apply_pipeline_snapshot(
        project_dir, stages, pipeline_file,
        str(getattr(args, "todo_id", "") or ""),
        bool(from_stage), logs_dir,
    )

    total = len(stages)
    print("=== Agentic Workflow: Starting Pipeline ===")
    print(f"Pipeline: {pipeline_file}")
    print(f"Stages: {' '.join(s.name for s in stages)}")
    print()
    _log(logs_dir, f"Pipeline started with {total} stages: {' '.join(s.name for s in stages)}")

    retry_counts = [0] * total

    # Start dashboard HTTP server (live updates, no file:// reload)
    # REPORTS29: rebinds the previous launch's port (state/dashboard_port
    # is the reuse token — the browser tab keeps its URL across
    # start/continue/run_next); a foreign-held port falls back to a new one.
    dashboard_port = 0
    _dashboard_server = None
    try:
        from .api.dashboard_server import start_dashboard_with_reuse

        dashboard_port, _dashboard_server = start_dashboard_with_reuse(project_dir)
        _log(logs_dir, f"Dashboard server: http://127.0.0.1:{dashboard_port}")
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

    # RUN3 #6: the single-launch no_checkpoints parameter — same pattern as
    # the timeout above: set at the process boundary, restored on EVERY exit
    # path so a later in-process launch (same MCP server process) sees a
    # clean environment. Background children already carry it in their env
    # from spawn (start_in_background); the getattr default keeps callers
    # without the attribute (old arg objects) working.
    _prev_no_checkpoints = os.environ.get("AWF_NO_CHECKPOINTS")
    if bool(getattr(args, "no_checkpoints", False)):
        os.environ["AWF_NO_CHECKPOINTS"] = "1"

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
                stage_name=s_name, stage_kind=s_kind,
                todo_id=current_todo, pipeline_pid=os.getpid(),
                # RUN6 #2: the name of the ACTUAL (resolved) pipeline —
                # the dashboard must draw the pipeline that really runs
                # (a run queue item can pin a non-default one). Cleared
                # with the rest of the state on exit (clear_state).
                pipeline=pipeline_file.stem,
                # dogfood-11: a new stage start means any previous salvage was
                # resolved (ACK/retry) — clear the flag, or the dashboard would
                # keep showing "Salvage" forever (write_state merges fields).
                salvage_needed=False, salvage_stage=None,
                # AUD02-01: same for the previous stage's signal — a stale
                # BLOCKED would re-trigger the blocked wake-up during a retried
                # stage before the worker emits its own signal.
                last_signal=None,
                # AUD02-03: same for the checkpoint keys — a stale
                # checkpoint_pending from a crashed/timed-out previous run
                # would make wait_for_event emit a phantom "checkpoint" event
                # for the NEW run (its one-shot POST server is long gone).
                # (AUD02-11: stage_role was written here but never read —
                # removed; readers compute the role from pipeline.yaml.)
                checkpoint_pending=False, checkpoint_port=None,
                checkpoint_form_url=None,
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

            # ORCH M3.3: the plan stage is where an unpinned start learns
            # its TODO — capture the unit's definition the moment it is
            # known. A pinned launch already carries the snapshot from
            # load time (the existence check makes this a no-op then); a
            # replan that yields a NEW TODO captures a fresh snapshot for
            # the new unit.
            if current_todo and not _pipeline_snapshot_file(
                project_dir, current_todo
            ).is_file():
                _write_pipeline_snapshot(
                    project_dir, stages, pipeline_file, current_todo, logs_dir
                )

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
        # SMO: write phase=done + preserve setup context for next iteration.
        # AUD02-04: normalized must survive the exit too — otherwise a
        # completed SMO project degrades to 'normalize' (or 'goal' without a
        # goal) >2h later, when detect_phase falls back to file detection.
        prev_state = read_state(project_dir) or {}
        clear_state(project_dir, logs_dir=logs_dir)
        write_state(
            project_dir,
            phase="done",
            goal=prev_state.get("goal"),
            normalized=prev_state.get("normalized"),
        )
        return 0
    finally:
        # P2/AUD04-10: restore env var (don't leak AWF_SUPERVISOR_TIMEOUT
        # to the caller — in-process foreground runs, MCP continue, tests).
        if agent_hard_timeout:
            if _prev_timeout is not None:
                os.environ["AWF_SUPERVISOR_TIMEOUT"] = _prev_timeout
            else:
                os.environ.pop("AWF_SUPERVISOR_TIMEOUT", None)
        # RUN3 #6: same save/restore for the single-launch bypass.
        if _prev_no_checkpoints is not None:
            os.environ["AWF_NO_CHECKPOINTS"] = _prev_no_checkpoints
        else:
            os.environ.pop("AWF_NO_CHECKPOINTS", None)
        # REPORTS29: stop the server (in-process consecutive launches must
        # rebind the port). The port file is NOT unlinked — it is the reuse
        # token for the next launch (start/continue/run_next rebind it, the
        # browser tab keeps its URL). A dead port is harmless:
        # _open_dashboard_sync liveness-checks the port before opening a
        # URL and falls back to file:// otherwise.
        from .api.dashboard_server import stop_dashboard_server

        stop_dashboard_server(_dashboard_server)
