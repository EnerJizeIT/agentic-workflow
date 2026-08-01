"""MCP tool implementations — awf workflow operations.

Each tool is a thin async wrapper around awf.api.* functions. Returns a dict
with at least ``status`` ("ok" or "error") + ``error`` message on failure.
On success, returns the corresponding ``Result.as_dict()`` payload with
``status: "ok"`` prepended.

These tools let opencode agents drive the agentic-workflow pipeline without
shell commands — directly through MCP protocol.
"""
from __future__ import annotations

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


async def awf_status(project_dir: str | None = None) -> dict[str, Any]:
    """Get current workflow state — active TODOs, progress, blocked, conflicts.

    Args:
        project_dir: Project root (default: current working directory).

    Returns:
        Dict with: project_name, active_todos (list of {todo_id, ack?,
        progress?}), done_count, blocked_count, blocked_ids,
        conflict_warning (str or None), suggestion (str or None).
    """
    try:
        result = api.get_status(_resolve_project_dir(project_dir))
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
    and returns immediately with a PID. The agent should then poll
    :func:`awf_status` to track progress. When a checkpoint is pending,
    open the appropriate form via ``open_form``.

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
        exit_code (foreground only), message.
    """
    try:
        result = api.start_pipeline(
            _resolve_project_dir(project_dir),
            background=background,
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
    try:
        result = api.continue_pipeline(
            _resolve_project_dir(project_dir),
            pipeline=pipeline,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
        )
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
        result = api.create_baseline(_resolve_project_dir(project_dir), todo_id)
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
    try:
        result = api.get_report(_resolve_project_dir(project_dir))
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
        result = api.load_supervisor_context(_resolve_project_dir(project_dir))
        return _ok(result)
    except api.AwfApiError as e:
        return _err(e)


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
