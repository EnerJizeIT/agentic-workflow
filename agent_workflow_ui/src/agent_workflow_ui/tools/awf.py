"""MCP tool implementations — awf workflow operations.

Each tool is a thin async wrapper around awf.api.* functions. Returns a dict
with at least ``status`` ("ok" or "error") + ``error`` message on failure.
On success, returns the corresponding ``Result.as_dict()`` payload with
``status: "ok"`` prepended.

These tools let opencode agents drive the agentic-workflow pipeline without
shell commands — directly through MCP protocol.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from awf import api

log = logging.getLogger(__name__)


def _ok(result: Any) -> dict[str, Any]:
    """Wrap a Result dataclass as a successful MCP response dict."""
    return {"status": "ok", **result.as_dict()}


def _err(e: api.AwfApiError) -> dict[str, Any]:
    """Wrap an AwfApiError as an error MCP response dict."""
    return {"status": "error", "error": str(e)}


def _resolve_project_dir(project_dir: str | None) -> Path:
    """Resolve project_dir argument; default to current working directory."""
    return Path(project_dir) if project_dir else Path.cwd()


async def _exec(api_fn: Any, /, **kwargs: Any) -> dict[str, Any]:
    """AUD-12.3: Standard try/except wrapper for all awf API calls."""
    try:
        result = await asyncio.to_thread(api_fn, **kwargs)
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Lifecycle ──────────────────────────────────────────────────────────


async def awf_init(
    project_dir: str | None = None,
    *,
    force: bool = False,
    project_name: str | None = None,
    test_cmd: str | None = None,
    lint_cmd: str | None = None,
    typecheck_cmd: str | None = None,
    build_cmd: str | None = None,
) -> dict[str, Any]:
    """Initialize .agentic/ in a project and assume the supervisor role.

    Creates the .agentic/ skeleton (config.yaml, supervisor.md, plan.md),
    auto-detects stack from package.json/pyproject.toml/etc., and returns
    the full supervisor context (role instructions, vision excerpt, plan.md
    content). Caller is now ready to act as supervisor.

    All command parameters are optional — auto-detected when not provided.
    Project name is derived from directory name when not provided.

    Args:
        project_dir: Project root (default: current working directory).
        force: Overwrite existing .agentic/ if present (default: False).
        project_name: Override auto-derived name (default: from dir name).
        test_cmd: Override auto-detected test command.
        lint_cmd: Override auto-detected lint command.
        typecheck_cmd: Override auto-detected typecheck command.
        build_cmd: Override auto-detected build command.

    Returns:
        Dict with: project_name, stack, vision_path, vision_excerpt,
        supervisor_md (full content), plan_md (full content),
        pipeline_configured (always False on fresh init), next_action
        (instructions for caller), warnings, created_files.
        On error: {status: "error", error: "..."}.
    """
    try:
        result = api.init_project(
            _resolve_project_dir(project_dir),
            force=force,
            project_name=project_name,
            test_cmd=test_cmd,
            lint_cmd=lint_cmd,
            typecheck_cmd=typecheck_cmd,
            build_cmd=build_cmd,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_status(project_dir: str | None = None) -> dict[str, Any]:
    """Get current workflow state — active TODOs, progress, blocked, conflicts.

    Args:
        project_dir: Project root (default: current working directory).

    Returns:
        Dict with: project_name, active_todos (list of {todo_id, ack?,
        progress?}), done_count, blocked_count, blocked_ids,
        conflict_warning (str or None), suggestion (str or None).
    """
    return await _exec(api.get_status, project_dir=_resolve_project_dir(project_dir))


# ─── Pipeline execution ─────────────────────────────────────────────────


async def awf_start(
    project_dir: str | None = None,
    *,
    background: bool = True,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Start the pipeline from the beginning.

    Default mode is ``background=True`` — launches a detached subprocess
    and returns immediately with a PID.

    **R6 sleep mode:** After start, call ``awf_open_pipeline_dashboard``
    to open the live dashboard, then go idle. Do NOT poll — the user
    monitors the dashboard and writes you when needed.

    Set ``background=False`` only for short pipelines or tests — the call
    blocks until completion (can be minutes/hours).

    Args:
        project_dir: Project root (default: cwd).
        background: Detach and return immediately (default: True).
        pipeline: Pipeline name to run (default: from config.yaml).
        from_stage: Start from a specific stage name.
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).

    Returns:
        Dict with: run_mode ("background"|"foreground"|"noop"),
        run_id (PID for background, None otherwise), log_file,
        exit_code (foreground only), message, dashboard_opened (bool).
    """
    result = await _exec(
        api.start_pipeline,
        project_dir=_resolve_project_dir(project_dir),
        background=background,
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    # SMO: deterministic dashboard opening — HTTP server started by orchestrator.
    # Opens HTTP URL (not file://) for smooth live updates via /api/state polling.
    if result.get("status") == "ok" and result.get("run_mode") == "background":
        try:
            import time as _time
            import webbrowser

            pd = _resolve_project_dir(project_dir)
            # Wait briefly for orchestrator to start dashboard server
            dashboard_url = None
            port_file = pd / ".agentic" / "state" / "dashboard_port"
            for _ in range(10):
                _time.sleep(0.5)
                if port_file.is_file():
                    try:
                        port = int(port_file.read_text().strip())
                        dashboard_url = f"http://127.0.0.1:{port}"
                        break
                    except (ValueError, OSError):
                        continue

            if dashboard_url:
                webbrowser.open(dashboard_url)
                result["dashboard_opened"] = True
                result["dashboard_url"] = dashboard_url
            else:
                # Fallback: generate static HTML + open file://
                from awf.api.dashboard import generate_dashboard
                await asyncio.to_thread(generate_dashboard, pd)
                dashboard_path = pd / ".agentic" / "dashboards" / "current.html"
                if dashboard_path.is_file():
                    webbrowser.open(f"file://{dashboard_path}")
                    result["dashboard_opened"] = True
        except Exception:
            result["dashboard_opened"] = False
    # SMO: explicit next_action — weak models need this to avoid polling.
    if result.get("status") == "ok" and result.get("run_mode") == "background":
        result["next_action"] = (
            "GO IDLE. Dashboard already opened. Do NOT call awf_wait_for_event "
            "or awf_status in a loop. Wait for the user to write you."
        )
    return result


async def awf_continue(
    project_dir: str | None = None,
    *,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Resume an interrupted pipeline. Finds newest active TODO and continues.

    Args:
        project_dir: Project root (default: cwd).
        pipeline: Pipeline name to run (default: from config.yaml).
        from_stage: Start from a specific stage name.
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).

    Returns:
        Same shape as :func:`awf_start`.
    """
    result = await _exec(
        api.continue_pipeline,
        project_dir=_resolve_project_dir(project_dir),
        pipeline=pipeline,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
    )
    if isinstance(result, dict) and result.get("run_mode") == "background":
        result["next_action"] = "Pipeline resumed. GO IDLE — wait for user."
    return result


async def awf_retry_stage(
    project_dir: str | None = None,
    *,
    pipeline: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Retry the current salvage stage — kill + continue in one call.

    P2: When pipeline is in salvage, supervisor calls this instead of
    manual kill + continue. Reads salvage_stage from state, kills pipeline,
    cleans salvage signals, restarts from that stage.

    Only works when state has salvage_stage (pipeline stopped on salvage).
    For other restart scenarios, use ``awf_kill`` + ``awf_continue``.

    Args:
        project_dir: Project root (default: cwd).
        pipeline: Pipeline name to run (default: from config.yaml).
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).

    Returns:
        Same shape as :func:`awf_start`.
    """
    result = await _exec(
        api.retry_stage,
        project_dir=_resolve_project_dir(project_dir),
        pipeline=pipeline,
        auto=auto,
        timeout=timeout,
    )
    if isinstance(result, dict) and result.get("run_mode") == "background":
        result["next_action"] = "Stage retried. GO IDLE — wait for user."
    return result


# ─── Baseline / rollback ────────────────────────────────────────────────


async def awf_baseline(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Create BASELINE-{todo_id}.{sha,status,tests.log,env.log} snapshot.

    Captures git HEAD SHA, working tree status, test output, and Python
    environment. Used for A1 commit isolation (final commit contains only
    diff vs baseline) and for rollback targets.

    Args:
        todo_id: TODO identifier (e.g. "TODO-0001").
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: todo_id, sha, is_git_repo, files_created (list),
        test_status ("passed"|"failed"|"no_test_cmd"|"no_config"),
        test_log_excerpt.
    """
    try:
        result = await asyncio.to_thread(
            api.create_baseline, _resolve_project_dir(project_dir), todo_id
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_rollback(
    todo_id: str,
    project_dir: str | None = None,
    *,
    mode: str = "hard",
) -> dict[str, Any]:
    """Rollback project to BASELINE-{todo_id}.sha.

    Modes:
    - ``hard`` (default): ``git reset --hard`` — discards all changes.
    - ``soft``: ``git reset`` — moves HEAD back, keeps working tree changes.
    - ``dry-run``: returns diff stat without modifying anything.

    After hard/soft rollback, an ACK file is written to .agentic/inbox/.

    Args:
        todo_id: TODO identifier whose baseline to rollback to.
        project_dir: Project root (default: cwd).
        mode: "hard" | "soft" | "dry-run" (default: "hard").

    Returns:
        Dict with: todo_id, baseline_sha, mode, ack_file (None for dry-run),
        diff_stat.
    """
    try:
        result = api.rollback(
            _resolve_project_dir(project_dir),
            todo_id,
            mode=mode,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Auto-commit approval ───────────────────────────────────────────────


async def awf_approve(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Approve auto-commit for a TODO in --auto mode.

    Creates APPROVE-{todo_id}.ready signal. If the pipeline is waiting for
    approval, it will commit and continue. Idempotent — safe to call
    multiple times.

    Args:
        todo_id: TODO identifier to approve.
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: todo_id, signal_file (path to APPROVE-*.ready).
    """
    try:
        result = api.approve_commit(_resolve_project_dir(project_dir), todo_id)
        response = _ok(result)
        # SMO: tell weak models to STOP calling approve (dogfood #4: 5x repeat)
        response["next_action"] = (
            f"{todo_id} approved and committed. Pipeline will EXIT after commit. "
            "For next TODO: awf_dispatch_todo → awf_start. "
            "DO NOT call awf_approve again."
        )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_reject(
    todo_id: str,
    reason: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Reject work at verify stage — creates REVIEW signal for replan.

    Writes REVIEW-{todo_id}.md to outbox + creates signal. Pipeline
    detects REVIEW → replan (supervisor writes new TODO).

    Args:
        todo_id: TODO identifier to reject.
        reason: Why the work is rejected (what needs fixing).
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: todo_id, review_file, next_action.
    """
    import re as _re
    from pathlib import Path as _Path

    if not todo_id:
        return {"status": "error", "error": "todo_id is required"}
    if not _re.match(r"^TODO-\d{4,}$", todo_id):
        return {"status": "error", "error": f"invalid todo_id '{todo_id}', expected TODO-NNNN"}
    if not reason.strip():
        return {"status": "error", "error": "reason is required"}

    try:
        pd = _resolve_project_dir(project_dir)
        outbox = _Path(pd) / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)

        review_file = outbox / f"REVIEW-{todo_id}.md"
        review_file.write_text(
            f"# REVIEW — {todo_id}\n\n## Reason\n{reason}\n",
            encoding="utf-8",
        )

        # awf_kill to stop the waiting pipeline
        try:
            api.kill_pipeline(pd)
        except Exception:
            pass

        return {
            "status": "ok",
            "todo_id": todo_id,
            "review_file": str(review_file),
            "next_action": (
                f"{todo_id} rejected. Pipeline killed. "
                "Fix the issues, then: awf_dispatch_todo → awf_start."
            ),
        }
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Reports ────────────────────────────────────────────────────────────


async def awf_report(project_dir: str | None = None) -> dict[str, Any]:
    """Generate workflow report — task statuses, git diff, latest test log.

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: project_name, generated_at, items (list of
        {todo_id, status: "OK"|"BLK"|"..."}), done_count, blocked_count,
        git_diff (str), latest_test_log_tail (str or None).
    """
    return await _exec(api.get_report, project_dir=_resolve_project_dir(project_dir))


# ─── Maintenance ────────────────────────────────────────────────────────


async def awf_reset(
    project_dir: str | None = None,
    *,
    tasks_only: bool = False,
    full: bool = False,
    orphans: bool = False,
) -> dict[str, Any]:
    """Clean runtime data.

    Modes (mutually exclusive):
    - ``tasks_only``: clean only inbox + outbox.
    - ``full``: clean inbox/outbox/context/logs/reports.
    - ``orphans``: remove orphan TODOs (active without progress).
    - default: clean inbox/outbox/context/logs/reports (keep phases).

    **Destructive** — clears runtime state. The ``orphans`` mode in CLI
    asks for confirmation; here we proceed (agent should ask user first
    via a form if confirmation is needed).

    Args:
        project_dir: Project root (default: cwd).
        tasks_only: Clean only inbox + outbox (default: False).
        full: Clean everything including context/logs/reports (default: False).
        orphans: Remove orphan TODOs (default: False).

    Returns:
        Dict with: cleaned_dirs (list), orphan_ids (list, only for
        orphans mode), mode ("full"|"default"|"tasks_only"|"orphans"|"noop").
    """
    try:
        result = api.reset_runtime(
            _resolve_project_dir(project_dir),
            tasks_only=tasks_only,
            full=full,
            orphans=orphans,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_add_role(
    name: str,
    project_dir: str | None = None,
    *,
    description: str = "",
    model: str = "",
) -> dict[str, Any]:
    """Generate a new role template at .agentic/roles/{name}.md.

    Creates a placeholder role file with sections for responsibility,
    input, actions, output, and prohibitions. Edit the file to specialize.

    Args:
        name: Role slug (e.g. "qa", "reviewer", "auditor").
        project_dir: Project root (default: cwd).
        description: One-line role description (default: "new role").
        model: Model ID for this role. If empty, placeholder inserted.

    Returns:
        Dict with: role_name, role_file (path), model.
    """
    try:
        result = api.add_role(
            _resolve_project_dir(project_dir),
            name,
            description=description,
            model=model,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_analyze_roles(
    project_dir: str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Analyze team roles for zone overlaps, optionally add disambiguation.

    Infers each role's "zone" (frontend/backend/test/docs/etc.) from
    keywords in the role .md. Detects pairs of roles claiming the same
    zone. For each role, builds a pipeline-specific disambiguation addendum
    that clarifies what THIS role should do (and what other roles cover).

    When ``dry_run=False`` (default), appends addenda to role .md files
    (idempotent — replaces existing BD-31 markers).

    Args:
        project_dir: Project root (default: cwd).
        dry_run: If True, return analysis without writing patches.

    Returns:
        Dict with: overlaps (list of {role_a, role_b, zone}),
        patches_applied (list of {role, preview}), dry_run (bool).
    """
    try:
        result = api.analyze_roles(
            _resolve_project_dir(project_dir),
            dry_run=dry_run,
        )
        response = _ok(result)
        response["next_action"] = "Roles analyzed. Call awf_confirm_normalized to advance to brief phase."
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Dogfood-2 automation: dispatch + context ────────────────────────────


async def awf_dispatch_todo(
    content: str,
    project_dir: str | None = None,
    *,
    role: str | None = None,
    todo_id: str | None = None,
) -> dict[str, Any]:
    """Atomically create a TODO, baseline it, dispatch the signal.

    Replaces the manual 3-step workflow (write md → awf_baseline → touch
    .ready). One call = TODO ready to be picked up by next pipeline run.

    Auto-picks next NNNN by scanning inbox + outbox (avoids collisions
    with already-completed TODOs). Creates BASELINE-NNNN.{sha,status,
    tests.log,env.log} snapshot. Writes TODO-NNNN.ready signal.

    Args:
        content: TODO markdown body (the task description, Mode A/B/C
            per supervisor.md).
        project_dir: Project root (default: cwd).
        role: Optional role hint for debugging. Stored as HTML comment
            in TODO .md. Does NOT affect pipeline routing (that's
            determined by stage order in pipeline.yaml).
        todo_id: Override auto-generated id (e.g. "TODO-0007"). Must
            match pattern TODO-NNNN.

    Returns:
        Dict with: todo_id, baseline_sha, role_hint, files_written (list
        of paths created).
    """
    try:
        result = api.dispatch_todo(
            _resolve_project_dir(project_dir),
            content,
            role=role,
            todo_id=todo_id,
        )
        response = _ok(result)
        # SMO: next_action guides weak models
        response["next_action"] = (
            f"{result.todo_id} dispatched. Call awf_start(background=True) to launch pipeline."
        )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_load_supervisor_context(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Aggregate everything a supervisor needs in one call.

    Replaces 5-6 separate tool calls at session start or before writing
    the next TODO. Returns in one payload:

    - vision_excerpt (first 4000 chars of vision/README)
    - plan_md content
    - supervisor_md content (role instruction)
    - active_todos + progress
    - done/blocked counts
    - pipeline_running + pid + log_tail
    - current_stage_name (extracted from log)
    - next_stage_role + its prohibitions excerpt (from pipeline.yaml)
    - last_signal (DONE/BLOCKED/REVIEW from log)
    - git_diff_stat

    Use cases:
    - Start of new supervisor session
    - After long pause (rebuild mental model)
    - Before writing next TODO (full context)

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with all fields of SupervisorContextResult.
    """
    try:
        result = await asyncio.to_thread(
            api.load_supervisor_context, _resolve_project_dir(project_dir)
        )
        response = _ok(result)
        # SMO: next_action from detected phase
        phase = result.phase if hasattr(result, 'phase') else 'unknown'
        _PHASE_NEXT = {
            "goal": "Ask user for goal → awf_set_goal.",
            "form": "Open project-setup form → awf_open_project_setup_form.",
            "normalize": "Run awf_analyze_roles → awf_confirm_normalized.",
            "brief": "Write BRIEF-TODO-NNNN.md → .ready signal.",
            "run": "Pipeline running. GO IDLE — wait for user.",
            "verify": "Read handoffs + git diff → awf_approve.",
            "done": "Pipeline complete. Ask user for next step.",
        }
        response["next_action"] = _PHASE_NEXT.get(phase, f"Phase: {phase}. Check awf_current_step.")
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Dogfood-5: shortcut tools (no more 'how do I fill the form?') ──────


async def awf_open_project_setup_form(
    project_dir: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Open project-setup form — no data needed, plugin auto-populates.

    Shortcut for ``open_form(template="project-setup", project_dir=...)``
    that hides the data-filling ceremony. Plugin automatically injects:
    - global skills (from ``~/.config/opencode/skills/``)
    - global custom roles (from ``~/.config/awf/roles/``)
    - available models (from ``opencode.json`` providers)
    - project-local roles (from ``.agentic/roles/*.md``)
    - existing supervisor variants + custom agent slugs (for conflict UX)

    Use this instead of ``open_form`` when configuring a project.
    **Do not study the template structure** or "what data does the form
    need" — this tool handles all of it. Just call it and wait for
    ``read_submit``.

    After user submits, plugin automatically materializes via
    ``awf.api.apply_project_setup`` (writes pipeline.yaml, patches
    config.yaml + supervisor.md). Use ``awf_status`` to verify, then
    ``awf_dispatch_todo`` for the first task.

    Args:
        project_dir: **Required.** Absolute path to awf project root.
            Must contain ``.agentic/`` (run ``awf_init`` first if missing).
        ttl_seconds: Auto-cancel form after N seconds (optional).

    Returns:
        Dict with form_id, browser_opened, submit_url. Same shape as
        ``open_form``.
    """
    from .forms import open_form

    # No data dict — plugin auto-populates everything for project-setup.
    # Passing empty dict triggers the template defaults + plugin's scan.
    result = await open_form(
        template="project-setup",
        data={},
        project_dir=project_dir,
        ttl_seconds=ttl_seconds,
    )
    # open_form returns its own dict (no "status" key by UI convention).
    # Wrap to match awf_* uniform contract: {"status": "ok", **result}.
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    return {"status": "ok", **result}


async def awf_open_increment_planning_form(
    variants: list[dict[str, Any]],
    project_dir: str,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    """Open increment-planning form — user picks decomposition variant.

    Replaces ad-hoc chat proposals ("here are 3 ways to decompose MVP,
    which one?") with a structured form. Supervisor generates variants
    (creative work — based on vision/requirements study), passes them
    to this tool. User sees cards with strategies, increments, pros/cons,
    picks one. Submit persists via ``awf.api.apply_increment_plan``.

    When to call:
    - After ``awf_load_supervisor_context`` revealed project state.
    - Before first ``awf_dispatch_todo`` — increment plan shapes the
      TODO sequence.
    - When MVP scope is large enough that decomposition matters (≥3
      artefacts needed). For 1-2 artefacts, skip — single TODO is fine.

    Variant schema (each item in ``variants`` list):
    ```
    {
        "id": "A",                          # short id, shown as badge
        "title": "Vertical slice: MVP in 3 cuts",
        "strategy": "vertical",             # vertical|horizontal|risk-first|...
        "description": "Each increment delivers user-visible value.",
        "estimated_todos": 3,               # how many TODOs this implies
        "estimated_time": "2 weeks",        # optional
        "risk_level": "low",                # optional
        "increments": [                     # ordered list
            {"name": "I1: storyboard only", "goal": "...", "artefacts": ["..."]},
            {"name": "I2: + Jira integration", "goal": "...", "artefacts": ["..."]}
        ],
        "pros": ["Fast feedback", "Each step demoable"],
        "cons": ["Refactoring overhead"]
    }
    ```

    Args:
        variants: list of 2-5 variant dicts (supervisor-generated).
        project_dir: **Required.** Absolute path to awf project root.
        ttl_seconds: Auto-cancel after N seconds (optional).

    Returns:
        Dict with form_id, browser_opened, submit_url.
    """
    if not variants or not isinstance(variants, list):
        return {
            "status": "error",
            "error": "variants must be a non-empty list of variant dicts",
        }
    if len(variants) > 6:
        return {
            "status": "error",
            "error": f"too many variants ({len(variants)}) — keep to 2-5 for user clarity",
        }

    from .forms import open_form

    result = await open_form(
        template="increment-planning",
        data={"variants": variants},
        project_dir=project_dir,
        ttl_seconds=ttl_seconds,
    )
    if "error" in result:
        return {"status": "error", "error": result["error"]}
    return {"status": "ok", **result}


# ─── DASH Phase 2: pipeline dashboard ────────────────────────────────────


async def awf_open_pipeline_dashboard(
    project_dir: str,
) -> dict[str, Any]:
    """Open pipeline dashboard in user's browser.

    Dashboard shows live pipeline progress: stages flow (visual chain),
    events stream, handoffs, task progress. Auto-refreshes every 5s.

    **R6: MANDATORY after awf_start.** Do NOT ask user "want to see
    dashboard?" — just open it. This is the user's monitoring tool while
    you are idle. After opening, go idle and respond to user messages.

    Dashboard is generated by orchestrator after each stage transition
    (written to ``.agentic/dashboards/current.html``). This tool just
    opens it.

    Args:
        project_dir: **Required.** Absolute path to awf project root.

    Returns:
        Dict with: status, dashboard_path, opened (bool).
    """
    p = _resolve_project_dir(project_dir)
    dashboard_path = p / ".agentic" / "dashboards" / "current.html"

    if not dashboard_path.is_file():
        # Generate on-demand if missing — but with a hard timeout guard
        # so MCP tool call doesn't block if generation is slow.
        try:
            from awf.api.dashboard import generate_dashboard

            result = generate_dashboard(p)
            if result is None:
                return {
                    "status": "error",
                    "error": "Dashboard generation returned None. Pipeline may not be running.",
                    "fallback": f"Open manually: xdg-open '{dashboard_path}'",
                }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Dashboard generation failed: {e}",
                "fallback": f"Pipeline may be running. Check: xdg-open '{dashboard_path}'",
            }

    # Open in browser via plugin's browser module
    from ..browser import open_path

    success, msg = open_path(dashboard_path)
    return {
        "status": "ok",
        "dashboard_path": str(dashboard_path),
        "opened": success,
        "message": msg,
    }


# ─── DASH Phase 3: supervisor wake-up (no more polling) ─────────────────


async def awf_wait_for_event(
    project_dir: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Check for pipeline events (reactive, NOT for proactive polling).

    R6 sleep mode: Do NOT call this in a loop after ``awf_start``.
    Instead: open dashboard → go idle → respond to user messages.
    Use this tool ONLY when the user writes you about a pipeline event
    (e.g. "pipeline finished", "salvage", "blocked") to get structured
    event details before acting.

    Returns immediately when:

    - ``verify`` — pipeline reached verify stage (supervisor must act)
    - ``blocked`` — worker wrote BLOCKED signal
    - ``checkpoint`` — BD-36 checkpoint form opened (tell user)
    - ``done`` — pipeline completed (state file cleared)
    - ``timeout`` — no event within timeout

    Args:
        project_dir: Project root (default: cwd).
        timeout: Max seconds to block (default 30 — MCP plugin single-thread limit).

    Returns:
        Dict with: event_type (verify/blocked/checkpoint/done/timeout/idle),
        message (instruction for supervisor), state_snapshot.
    """
    try:
        result = await asyncio.to_thread(
            api.wait_for_event,
            _resolve_project_dir(project_dir),
            timeout=timeout,
        )
        response = _ok(result)
        # SMO: next_action per event_type — weak models need explicit guidance
        et = result.get("event_type", "timeout") if isinstance(result, dict) else "timeout"
        _EVENT_ACTIONS = {
            "verify": "Pipeline at verify. Read handoffs + git diff → awf_approve.",
            "blocked": "Worker blocked. Read BLOCKED note → replan or adjust TODO.",
            "checkpoint": "Checkpoint form opened in browser. Tell user to approve.",
            "done": "Pipeline complete. Ask user for next step.",
            "salvage": "Salvage needed. Read SALVAGE note → awf_retry_stage or ACK.",
            "timeout": "No event. DO NOT call awf_wait_for_event again. Wait for user.",
        }
        response["next_action"] = _EVENT_ACTIONS.get(et, "Check awf_status, then wait for user.")
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Model configuration validation (dogfood-10) ─────────────────────────


async def awf_check_model_config(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Check that models in config.yaml have valid providers in opencode.json.

    Prevents silent fallback to wrong model. Returns per-role model
    validation + warnings if provider not found.

    Call BEFORE ``awf_start`` to catch configuration issues:
    - Provider not in opencode.json → worker silently uses cloud fallback
    - Model ID not in provider's models list → similar fallback
    - Missing models → worker uses opencode default

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: models (list of {role, model, provider, valid, note}),
        warnings (list of strings), providers_available (list).
    """
    try:
        result = await asyncio.to_thread(
            api.check_model_config, _resolve_project_dir(project_dir)
        )
        return {"status": "ok", **result}
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}

# ─── Kill pipeline (SELF-2) ─────────────────────────────────────────────


async def awf_kill(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Kill running pipeline cleanly.

    Reads pipeline_pid from state, sends SIGTERM, waits 5s, SIGKILL if
    still alive, clears state. Use when pipeline is stuck or needs to stop.

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: killed (bool), pid, message.
    """
    try:
        result = await asyncio.to_thread(
            api.kill_pipeline, _resolve_project_dir(project_dir)
        )
        return {"status": "ok", **result}
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── SMO: Phase-aware supervisor tools ─────────────────────────────────


async def awf_current_step(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Determine current supervisor phase and return compact prompt.

    SMO: Replaces loading 600+ line supervisor.md. Detects phase from
    project state (init/goal/form/normalize/brief/run/verify) and returns
    only the relevant section + core invariants (~50-100 lines).

    Call this at the START of every interaction to know what to do.

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: phase (str), prompt (str), goal (str|None).
    """
    try:
        from awf.phase import detect_phase, get_phase_prompt
        from awf.pipeline_state import read_state

        pd = _resolve_project_dir(project_dir)
        phase = await asyncio.to_thread(detect_phase, pd)
        prompt = await asyncio.to_thread(get_phase_prompt, phase, pd)
        state = await asyncio.to_thread(read_state, pd)
        goal = state.get("goal") if state else None

        return {
            "status": "ok",
            "phase": phase,
            "prompt": prompt,
            "goal": goal,
        }
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_set_goal(
    goal: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Set session goal and advance to form phase.

    SMO.3: Called after supervisor elicited goal from user. Stores goal
    in state and advances phase: goal → form.

    Args:
        goal: User's goal for this session (1-3 sentences).
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: phase ("form"), goal (str).
    """
    try:
        from awf.phase import advance_phase

        pd = _resolve_project_dir(project_dir)
        new_phase = await asyncio.to_thread(
            advance_phase, pd, goal=goal
        )
        # SMO: next_action guides weak models — don't let them guess
        _NEXT = {
            "form": "Открой project-setup форму через awf_open_project_setup_form.",
            "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
            "brief": "Напиши BRIEF-TODO-NNNN.md для user approval.",
        }
        return {
            "status": "ok", "phase": new_phase, "goal": goal,
            "next_action": _NEXT.get(new_phase, f"Phase: {new_phase}"),
        }
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_confirm_normalized(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Confirm skills normalization is done, advance to brief phase.

    SMO.5: Called after supervisor completed 3-part normalization checklist.
    Advances phase: normalize → brief. Awf does NOT let supervisor proceed
    until this is called (gate).

    Args:
        project_dir: Project root (default: cwd).

    Returns:
        Dict with: phase ("brief").
    """
    try:
        from awf.phase import advance_phase

        pd = _resolve_project_dir(project_dir)
        new_phase = await asyncio.to_thread(
            advance_phase, pd, normalized=True
        )
        # SMO: next_action guides weak models
        _NEXT = {
            "brief": "Изучи vision и BACKLOG, вызови awf_dispatch_todo с задачей.",
            "run": "Проверь awf_status, при необходимости dispatch_todo + awf_start.",
        }
        return {
            "status": "ok", "phase": new_phase,
            "next_action": _NEXT.get(new_phase, f"Phase: {new_phase}"),
        }
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}
