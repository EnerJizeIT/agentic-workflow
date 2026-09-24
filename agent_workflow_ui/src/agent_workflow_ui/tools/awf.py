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

from awf import api, git_utils

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

    **R1 warning (AUD05-04):** if ``.agentic/`` already exists and
    ``force=False``, the runtime directories (inbox/outbox/handoff/done/
    state/logs/context/dashboards/inputs) are DELETED and config.yaml is
    preserved — calling this on a live project drops active TODOs and
    state. ``dry_run=True`` is a pure read.

    All command parameters are optional — auto-detected when not provided.
    Project name is derived from directory name when not provided.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: project_name, active_todos (list of {todo_id, ack?,
        progress?}), done_count, blocked_count, blocked_ids,
        conflict_warning (str or None), suggestion (str or None),
        next_action (the supervisor's next step derived from the state).
    """
    result = await _exec(api.get_status, project_dir=_resolve_project_dir(project_dir))
    # RUN6 #5 (TODO-0060): every answer leads to the next step.
    if isinstance(result, dict) and result.get("status") == "ok" and not result.get("next_action"):
        from awf.brief import next_action_from_status

        result["next_action"] = next_action_from_status(result)
    return result


# ─── Pipeline execution ─────────────────────────────────────────────────


async def awf_start(
    project_dir: str | None = None,
    *,
    background: bool = True,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
    todo_id: str = "",
    no_checkpoints: bool = False,
) -> dict[str, Any]:
    """Start the pipeline from the beginning.

    Default mode is ``background=True`` — launches a detached subprocess
    and returns immediately with a PID.

    **R6 sleep mode:** ``awf_start`` opens the dashboard itself — check
    ``dashboard_opened`` in the response and call
    ``awf_open_pipeline_dashboard`` only ONCE as a fallback when it is
    false. Then go idle. Do NOT poll — the user monitors the dashboard
    and writes you when needed.

    Set ``background=False`` only for short pipelines or tests — the call
    blocks until completion (can be minutes/hours).

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        background: Detach and return immediately (default: True).
        pipeline: Pipeline name to run (default: from config.yaml).
        from_stage: Start from a specific stage name.
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).
        todo_id: Pin the pipeline to a specific TODO (e.g. "TODO-0015").
            Without it the engine picks the "newest active" TODO — wrong
            pick when several TODOs are active (AUD08-02).
        no_checkpoints: RUN3 #6 — when True, the BD-36 plan checkpoint form
            is skipped for THIS launch only (single start, not a run).
            Process-scoped: not written to config/state, the next launch
            behaves as before.

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
        # AUD08-02: signature parity with api.start_pipeline / CLI --todo.
        todo_id=todo_id,
        no_checkpoints=no_checkpoints,
    )
    # SMO: deterministic dashboard opening — HTTP server started by
    # orchestrator. AUD08-05: one shared implementation, run in a worker
    # thread (no time.sleep on the MCP event loop).
    if result.get("status") == "ok" and result.get("run_mode") == "background":
        try:
            dash = await asyncio.to_thread(
                _open_dashboard_sync, _resolve_project_dir(project_dir), True
            )
            result["dashboard_opened"] = dash["opened"]
            if dash["method"] == "http":
                result["dashboard_url"] = dash["url"]
        except Exception:
            result["dashboard_opened"] = False
    # SMO: explicit next_action — weak models need this to avoid polling.
    # AUD08-15: the text must agree with the fact in `dashboard_opened` —
    # a failed open (headless) used to be claimed as "already opened".
    if result.get("status") == "ok" and result.get("run_mode") == "background":
        if result.get("dashboard_opened"):
            result["next_action"] = (
                "GO IDLE. Dashboard already opened. Do NOT call awf_wait_for_event "
                "or awf_status in a loop. Wait for the user to write you."
            )
        else:
            result["next_action"] = (
                "GO IDLE. Dashboard did NOT open (headless?) — open it once with "
                "awf_open_pipeline_dashboard(project_dir). Do NOT call "
                "awf_wait_for_event or awf_status in a loop. Wait for the user "
                "to write you."
            )
    # RUN6 #5 (TODO-0060): foreground/noop paths also lead to the next step.
    if result.get("status") == "ok" and result.get("run_mode") == "foreground":
        result["next_action"] = (
            f"Pipeline finished (exit_code={result.get('exit_code')}). Check "
            "awf_status and act on the last signal (verify → the verify ritual)."
        )
    elif result.get("status") == "ok" and result.get("run_mode") == "noop":
        result["next_action"] = (
            f"No pipeline launched ({result.get('message', '')}) — check "
            "awf_status, then awf_dispatch_todo for the next unit."
        )
    return result


async def awf_continue(
    project_dir: str | None = None,
    *,
    pipeline: str | None = None,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
    ack: str = "",
    todo_id: str = "",
    background: bool = True,
    no_checkpoints: bool = False,
) -> dict[str, Any]:
    """Resume an interrupted pipeline, pinned to the right unit.

    Unit resolution: explicit ``todo_id`` > ``ack`` > state > newest active
    TODO. The answer names the unit it resumes.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        pipeline: Pipeline name to run (default: from config.yaml).
        from_stage: Start from a specific stage name.
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).
        ack: Accept a BLOCKED TODO and resume (e.g. "TODO-0009"). Writes
            ACK-{todo}.ready and clears the BLOCKED closure — the supervisor's
            answer survives process death. The ack also PINS its own unit:
            the resumed TODO is the acked one, not "newest active". Without
            it, a pending ACK/APPROVE is still picked up; a blocked TODO with
            no answer returns a clear instruction instead of "No active TODO
            found".
        todo_id: Explicit pin — the TODO to resume (same as awf_start).
            Wins over the ack, the state and "newest active". When only
            ``ack`` is set, the pin is taken from the ack.
        background: Detach and return immediately (default: True, the API
            default). Set ``background=False`` to block the call until the
            pipeline finishes (AUD08-02 — foreground continue was previously
            impossible from MCP).
        no_checkpoints: RUN3 #6 — when True, the BD-36 plan checkpoint form
            is skipped for THIS launch only (single continue, not a run).
            Process-scoped: not written to config/state, the next launch
            behaves as before.

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
        ack=ack,
        todo_id=todo_id,
        # AUD08-02: signature parity with api.continue_pipeline.
        background=background,
        no_checkpoints=no_checkpoints,
    )
    if isinstance(result, dict) and result.get("run_mode") == "background":
        # AUD08-05: shared dashboard-open in a worker thread (no loop block).
        try:
            dash = await asyncio.to_thread(
                _open_dashboard_sync, _resolve_project_dir(project_dir), True
            )
            result["dashboard_opened"] = dash["opened"]
        except Exception:
            pass
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
    cleans salvage signals, restarts from that stage — pinned to the
    salvaged TODO (state ``todo_id``, fallback: the SALVAGE note filename),
    so it restarts exactly the salvage unit, not "newest active". The
    answer names the restarted ``todo_id``.

    Only works when state has salvage_stage (pipeline stopped on salvage).
    For other restart scenarios, use ``awf_kill`` + ``awf_continue``.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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
        # AUD08-05: shared dashboard-open in a worker thread (no loop block).
        try:
            dash = await asyncio.to_thread(
                _open_dashboard_sync, _resolve_project_dir(project_dir), True
            )
            result["dashboard_opened"] = dash["opened"]
        except Exception:
            pass
        result["next_action"] = "Stage retried. GO IDLE — wait for user."
    return result


# ─── Autonomous run (забег) — SPEC A-run v1 ─────────────────────────────


async def awf_run_start(
    project_dir: str | None = None,
    *,
    queue: list[str | dict[str, Any]] | None = None,
    budget_minutes: int = 0,
    stop_flags_json: str = "",
    note: str = "",
    force: bool = False,
    no_checkpoints: bool = False,
) -> dict[str, Any]:
    """Start an autonomous run: a queue of TODOs with mechanical gates.

    The supervisor chat drives the loop; awf keeps the state and enforces
    the stop-list (queue/budget/stop-flags/rejects). The owner is pinged
    only on stop conditions or at the end.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        queue: Ordered queue items. Each item is either a TODO id string
            (e.g. "TODO-0010" — the pipeline comes from config) or an object
            {"todo_id": "TODO-0023", "pipeline": "audit-llm"} that pins the
            item's own pipeline (RUN3 #2). Mixed lists are allowed.
        budget_minutes: Optional time budget (0 = unlimited).
        stop_flags_json: Optional JSON map of TODO id → [reason], e.g.
            '{"TODO-0012": ["phase-boundary", "external-audit"]}'. awf refuses
            to auto-continue past a flagged item — the run stops there.
        note: R5 — one-line "what is happening now" for the owner's dashboard.
            Keep it fresh with awf_run_note on every stage change.
        force: Replace an already-active run state (recovery from a stale or
            wrong-directory run). Without it a second run_start is refused.
        no_checkpoints: B4 — when True, the BD-36 plan checkpoint form is
            skipped for every pipeline launch of this run. Use for autonomous
            runs where the owner does not sit at every TODO. The flag is
            stored in the run state and shown by awf_run_status.

    Returns:
        Dict with: active, queue, position, budget, stop_flags, next_action.
    """
    import json as _json

    flags: dict[str, list[str]] = {}
    if stop_flags_json.strip():
        try:
            parsed = _json.loads(stop_flags_json)
        except (_json.JSONDecodeError, TypeError):
            return {"status": "error", "error": "stop_flags_json is not valid JSON."}
        if not isinstance(parsed, dict):
            # AUD08-16: valid JSON that is not a map (e.g. a list like
            # ["TODO-0012"]) used to be dropped silently — the run started
            # with NO stop flags while the owner believed the gate was set.
            return {
                "status": "error",
                "error": (
                    "stop_flags_json must be a JSON object "
                    '{TODO-NNNN: [reason]}, e.g. \'{"TODO-0012": ["phase-boundary"]}\' '
                    f"— got {type(parsed).__name__}"
                ),
            }
        flags = {str(k): list(v) for k, v in parsed.items()}
    result = await _exec(
        api.run_start,
        project_dir=_resolve_project_dir(project_dir),
        queue=queue or [],
        budget_minutes=budget_minutes,
        stop_flags=flags,
        note=note,
        force=force,
        no_checkpoints=no_checkpoints,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            "Write each TODO before its turn, then awf_run_next. Keep the run "
            "note fresh with awf_run_note(text=...) at every stage change — "
            "it is what the owner sees on the dashboard."
        )
    return result


async def awf_run_note(
    project_dir: str | None = None,
    *,
    text: str = "",
) -> dict[str, Any]:
    """Set the run's live description line for the owner's dashboard (R5).

    The supervisor owns this text; the dashboard renders it under the run
    chip and in the Итерация tab, so the owner sees whether the run is alive
    without asking. Update it on every stage change.
    """
    return await _exec(
        api.run_note,
        project_dir=_resolve_project_dir(project_dir),
        text=text,
    )


async def awf_restore(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Restore an archived TODO from done/{id}/ back to the inbox (active).

    Safety net (NEG-2026-09-19 R2a): manual recovery for a TODO that was
    archived without work. TODO.md returns, .ready is re-created, handoff
    files move back; PROGRESS/DONE history stays in done/.
    """
    result = await _exec(
        api.restore_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            f"{result.get('todo_id', 'TODO')} is back in the inbox (active) — "
            "awf_start(project_dir, todo_id=...) or awf_run_next in a run."
        )
    return result


async def awf_unblock(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Clear stale BLOCKED/ACK closure signals so a re-issued TODO is active again.

    RUN3 #4: a stale ``outbox/BLOCKED-<id>.ready`` outlives a re-issue and
    keeps ``todos.is_closed`` true — ``awf_status`` shows an empty list and
    ``awf_start`` without a pin answers "No active TODO". This moves the
    closure signals (canonical and legacy forms) to a ``context/`` trace
    directory. DONE closures are never touched — an archived TODO comes
    back only via ``awf_restore``. Refused while the pipeline is running.
    """
    result = await _exec(
        api.unblock_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            f"{result.get('todo_id', 'TODO')} is active again — "
            "awf_start(project_dir, todo_id=...) or awf_run_next in a run."
        )
    return result


async def awf_todo_remove(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Remove a TODO that never started; the file keeps a trace in done/.

    RUN3 #5: an inert TODO (``.md`` without ``.ready``/signals/progress)
    moves to ``done/<id>/removed-<timestamp>.md``. Refused when a
    ``.ready`` or any signal/progress exists (hints: ``awf_unblock`` /
    ``awf_reset(orphans=True)``).
    """
    result = await _exec(
        api.remove_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            f"Unit {result.get('todo_id', '?')} removed (trace in {result.get('trace_path', 'done/')}). "
            "Still needed — re-dispatch via awf_dispatch_todo (a new number)."
        )
    return result


async def awf_todo_retire(
    todo_id: str,
    reason: str = "",
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Retire a rejected/abandoned TODO that stays "active" (RUN5 #2).

    The reject path writes ``DONE-{id}.{md,json}`` to the outbox WITHOUT the
    ``DONE-{id}.ready`` signal, so ``todos.is_closed`` stays false and
    ``awf_status``/``awf_brief`` keep listing the TODO as active forever
    (battle case TODO-0035). This moves the TODO's files (``.md``,
    ``.ready``, ``PROGRESS-*``, ``DONE-*.md/.json`` without ``.ready``,
    ``REVIEW-*``) to ``done/<id>/`` and writes a ``RETIRED-<timestamp>.md``
    note with the reason — no fake closure signal is written, and
    ``awf_restore`` still brings the TODO back.

    Refusals: no TODO file in inbox or done/; already archived
    (``done/<id>/TODO.md``); a live pipeline on this id (kill/wait first);
    empty ``reason`` (required — it is the RETIRED note body).
    """
    result = await _exec(
        api.retire_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
        reason=reason,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            f"Unit {result.get('todo_id', '?')} retired (RETIRED note: "
            f"{result.get('retired_note', 'done/<id>/')}). Still needed — "
            "re-dispatch via awf_dispatch_todo; bring this one back — awf_restore."
        )
    return result


async def awf_todo_update(
    todo_id: str,
    content: str = "",
    project_dir: str | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Reword a not-started TODO, keeping the number (RUN6 #4).

    Replaces the content of ``inbox/TODO-<id>.md`` in place — the number,
    the dispatch ``.ready`` and the baseline stay untouched (the baseline
    pins a git sha, not the text). The previous content is backed up to
    ``context/TODO-<id>.md.bak-<timestamp>``. Refusals: no TODO file in
    the inbox; empty ``content``; a started TODO (PROGRESS/signals/
    closure — fix the unit via REVIEW/replan, or retire + re-dispatch);
    a live pipeline on this id.
    """
    result = await _exec(
        api.update_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
        content=content,
        reason=reason,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            f"{result.get('todo_id', 'TODO')} reworded (backup: "
            f"{result.get('backup', 'context/')}), the unit stays ready — "
            "awf_start / awf_run_next."
        )
    return result


async def awf_tree_sha(project_dir: str | None = None) -> dict[str, Any]:
    """Compute the working-tree fingerprint for ``awf_approve(verified_sha=...)``.

    Same semantics as the CLI ``awf tree-sha`` (``awf.git_utils.
    tree_fingerprint``): HEAD + every tracked change + untracked files;
    same tree → same sha, at any time. Non-repo or a repo without
    commits → ``{status: "error", error: ...}`` (the CLI's message).
    """
    try:
        sha = await asyncio.to_thread(
            git_utils.tree_fingerprint, _resolve_project_dir(project_dir)
        )
    except RuntimeError as e:
        msg = str(e).splitlines()[0] if str(e) else "git failure"
        return {
            "status": "error",
            "error": (
                f"cannot compute the tree fingerprint: {msg}. Is the "
                "project a git repo with at least one commit?"
            ),
        }
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}
    return {"status": "ok", "sha": sha}


async def awf_run_status(project_dir: str | None = None) -> dict[str, Any]:
    """Show the current run (забег) state: position, budget left, rejects, stop reason."""
    result = await _exec(api.run_status, project_dir=_resolve_project_dir(project_dir))
    if isinstance(result, dict) and result.get("status") == "ok":
        if result.get("active"):
            result["next_action"] = (
                f"Run active (position {result.get('position', '?')}) — "
                "awf_run_next(project_dir) launches the next queued unit."
            )
        else:
            result["next_action"] = (
                "No active run — awf_run_start(project_dir, queue=[...]) to "
                "start one, or awf_brief for the onboarding card."
            )
    return result


async def awf_run_next(
    project_dir: str | None = None,
    *,
    from_stage: str | None = None,
    timeout: int = 3600,
) -> dict[str, Any]:
    """Launch the next queue item, or stop when a gate fires.

    Refused when the TODO file is missing (write it first), the previous TODO
    is not finished, or no run is active. Stopped (with a report) on queue
    exhausted / budget / stop flag / two rejections.

    Returns:
        Dict with: action (started/stopped/refused), todo_id, message,
        run_mode, run_id, log_file, report_file, next_action.

        AUD08-07: wrapped in ``_exec`` so an AwfApiError/any exception
        degrades to ``{status: "error", error: ...}`` instead of escaping
        the tool (this was the only awf_ tool without the error wrapper).
    """
    result = await _exec(
        api.run_next,
        project_dir=_resolve_project_dir(project_dir),
        from_stage=from_stage,
        background=True,
        timeout=timeout,
    )
    response = result  # _exec returns {status: ok|error, ...as_dict()}
    if response.get("status") == "ok" and response.get("action") == "started":
        # AUD08-05: shared dashboard-open in a worker thread (no loop block).
        try:
            dash = await asyncio.to_thread(
                _open_dashboard_sync, _resolve_project_dir(project_dir), True
            )
            response["dashboard_opened"] = dash["opened"]
        except Exception:
            pass
    return response


async def awf_run_finish(
    project_dir: str | None = None,
    *,
    reason: str = "finished by supervisor",
    summary: str = "",
) -> dict[str, Any]:
    """Close the run: write RUN-REPORT-{ts}.md to outbox and mark inactive."""
    result = await _exec(
        api.run_finish,
        project_dir=_resolve_project_dir(project_dir),
        reason=reason,
        summary=summary,
    )
    if isinstance(result, dict) and result.get("status") == "ok":
        result["next_action"] = (
            "Run closed (RUN-REPORT in the outbox). Next unit — "
            "awf_dispatch_todo, or a new awf_run_start if the queue continues."
        )
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: todo_id, sha, is_git_repo, files_created (list),
        test_status ("passed"|"failed"|"no_test_cmd"|"no_config"),
        test_log_excerpt, next_action.
    """
    try:
        result = await asyncio.to_thread(
            api.create_baseline, _resolve_project_dir(project_dir), todo_id
        )
        response = _ok(result)
        if not response.get("next_action"):
            response["next_action"] = (
                f"Baseline pinned (sha {str(response.get('sha', '?'))[:8]}). Next: "
                f"awf_start(project_dir, todo_id='{response.get('todo_id', 'TODO')}'). "
                "awf_rollback is the way back to this sha."
            )
        return response
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
    """Roll the project back to BASELINE-{todo_id}.sha.

    Modes:
    - ``hard`` (default): ``git reset --hard`` — discards all changes.
    - ``soft``: ``git reset`` — moves HEAD back, keeps working tree changes.
    - ``dry-run``: returns diff stat without modifying anything.

    After hard/soft rollback, an ACK file is written to .agentic/inbox/.

    Args:
        todo_id: TODO identifier whose baseline to rollback to.
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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
        response = _ok(result)
        sha = str(response.get("baseline_sha", "?"))[:8]
        if response.get("mode") == "dry-run":
            response["next_action"] = (
                f"Preview only — nothing changed. To roll back: awf_rollback("
                f"todo_id, mode='hard'|'soft') (target sha {sha})."
            )
        else:
            response["next_action"] = (
                f"Rolled back to {sha}. Next: re-dispatch the unit "
                "(awf_dispatch_todo) or adjust the plan."
            )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_prove_red(
    todo_id: str,
    project_dir: str | None = None,
    *,
    tests: list[str] | None = None,
) -> dict[str, Any]:
    """Prove the declared tests are red on the baseline sha (U4).

    Deploys BASELINE-{todo_id}.sha into a temporary git worktree under
    /tmp/opencode/, copies the test files (new/untracked included) from the
    current tree, and runs them: on the old code the tests must fail with a
    real red (assertions), and the same tests must pass in the current tree.

    Verdicts (same as the CLI ``awf prove-red`` exit codes):
    - ``red-ok`` (0) — real red on baseline, green in the current tree;
    - ``not-red`` (1) — tests PASSED on the baseline: they prove nothing;
    - ``broken-runner`` (2) — 0 tests collected / collection error / broken
      runner / import error that is only a missing project symbol (the last
      one is acceptable for new code — a warning is attached);
    - ``green-after`` (1) — red on baseline but not passing in the current
      tree.

    The worktree is removed in ``finally`` on every outcome.

    Args:
        todo_id: TODO identifier (e.g. "TODO-0021").
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        tests: Test files and/or ``file::test`` ids. Default: the
            ``prove_red`` block of the TODO contract.

    Returns:
        Dict with: todo_id, verdict, exit_code, baseline_sha, tests,
        copied_files, baseline_output, current_output, message, warnings.
        On error: {status: "error", error: "..."}.
    """
    return await _exec(
        api.prove_red,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
        tests=tests,
    )


async def awf_verify_pack(
    todo_id: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Produce one deterministic verify report for the supervisor (U5).

    Runs the mechanical part of the verify ritual and writes
    ``.agentic/context/GATES-{todo_id}.md`` (markdown + JSON block):
    fast gates (contracts / ratchet / instruction-gates — NOT the full
    test suite, that belongs to QA), safety canaries (``pid > 1`` guard,
    ``_forbid_session_kill`` tripwire, regression test), diff minimality
    vs ``BASELINE-{todo_id}.sha``, the contract's ``verify:`` commands,
    prove-red (when declared), DONE.json facts (executor data), and
    ``ruff check .``.

    Verdicts: exit 0 = all measured checks ok, 1 = failures found,
    2 = nothing was measured (no baseline, no contract, no gates).

    Args:
        todo_id: TODO identifier (e.g. "TODO-0022").
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: todo_id, verdict, exit_code, measured, report_path,
        sections (name -> status), details.
        On error: {status: "error", error: "..."}.
    """
    return await _exec(
        api.verify_pack,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
    )


# ─── Metrics (U8) ───────────────────────────────────────────────────────


async def awf_metrics(
    project_dir: str | None = None,
    *,
    reference_model: str | None = None,
    since: str | None = None,
    out: str | None = None,
    refresh_subscriptions: bool = False,
    mirror: bool = True,
    all_projects: bool = False,
) -> dict[str, Any]:
    """Collect token/cost metrics of the work program and write the report (U8).

    Aggregates worker sessions (titles ``awf-<role>-TODO-NNNN``) and
    supervisor sessions (config ``metrics.supervisor_titles``) from
    opencode.db, unit windows (baseline sha → verify commit), code lines
    per unit (git shortstat), and the cost conversion: "if workers had
    run on <reference model>, the cost would be $Y" (models.dev prices).

    RUN10 #3-fix: by default only the current project's sessions are
    collected — a session belongs to the project when its ``part.data``
    contains the project path (``session.directory`` is not a
    discriminator: workers and the supervisor run from $HOME); the report
    header names the scope. ``all_projects=True`` — the whole shared
    opencode.db, data mixed (the pre-RUN10 behavior).

    The markdown report is written by default to ``metrics.output_dir``
    (default: ~/Desktop) as ``awf-metrics-<YYYYMMDD-HHMM>.md``.

    U8c: the report can be mirrored to an archive location (config
    ``metrics.mirror_dir``) — the copy lands there after the report is
    written successfully (copy failure is a warning, not an error).

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        reference_model: Model id for the cost conversion
            (default: config ``metrics.reference_model`` /
            ``anthropic/claude-sonnet-4-6``).
        since: Start of the statistics window (ISO date/datetime or epoch;
            default: config ``metrics.since``, no filter).
        out: Output file or directory (default: ``metrics.output_dir``).
        refresh_subscriptions: U8b — update the subscriptions table from
            config ``metrics.subscriptions_url`` before computing
            (fallback to the built-in table on failure).
        mirror: U8c — copy the report to config ``metrics.mirror_dir``
            after it is written (default: True; pass False to skip the
            copy for this run).
        all_projects: RUN10 #3 — collect sessions from all projects of the
            shared opencode.db instead of only the current project
            (default: False — current project only).

    Returns:
        Dict with: status, report_path, mirror_path, units, totals,
        workers_by_role, supervisor_outside, conversion (line + costs),
        warnings, exit_code (0 = measured something, 1 = nothing
        measurable).
        On error: {status: "error", error: "..."}.
    """
    return await _exec(
        api.collect_metrics,
        project_dir=_resolve_project_dir(project_dir),
        reference_model=reference_model,
        since=since,
        out=out,
        refresh_subscriptions=refresh_subscriptions,
        mirror=mirror,
        all_projects=all_projects,
    )


# ─── Feedback contour (RUN4 #2) ─────────────────────────────────────────


async def awf_feedback(
    project_dir: str | None = None,
    type: str = "",
    title: str = "",
    *,
    body: str = "",
    severity: str = "",
    expected: str = "",
    got: str = "",
    why: str = "",
    proposal: str = "",
    stdout: bool = False,
) -> dict[str, Any]:
    """Write a bug/feature report about awf friction to the owner (RUN4 #2).

    The feedback contour: friction with the tool becomes a structured
    report on the owner's desktop (config ``feedback.dir``, default
    ``~/Desktop``). The report is assembled automatically — header facts
    (awf version, project, phase, run position/no_checkpoints, current
    task, awf-repo git sha best-effort, date), the skeleton
    «Что пытался / Ожидал / Что получил / Почему мешает / Предложение»
    (``body`` fills «Что пытался», ``expected``/``got``/``why``/
    ``proposal`` fill the rest), and the tail of the project's newest
    log (<=20 lines). EMPTY sections are not printed at all — a one-text
    report has no empty headings (RUN10 #2). File:
    ``awf-<bug|feature>-<YYYYMMDD>-<slug>.md``; a repeat on the same day
    with the same slug gets a ``-2`` suffix.
    Secrets: the report never reads the environment.

    Do not stay silent: silence does not fix the tool.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        type: ``bug`` or ``feature``.
        title: One-line title (becomes the file slug; ASCII passes,
            Cyrillic is transliterated, letters-only fallback ``report``).
        body: Text for the «Что пытался» section.
        severity: ``low`` / ``medium`` / ``high`` (empty = no mark).
        expected: Text for the «Ожидал» section (empty = not printed).
        got: Text for the «Что получил» section (empty = not printed).
        why: Text for the «Почему мешает» section (empty = not printed).
        proposal: Text for the «Предложение» section (empty = not printed).
        stdout: True — return the report text, write no file.

    Returns:
        Dict with: status, file ("" when stdout), report_dir, slug,
        ftype, report (full text).
        On error: {status: "error", error: "..."}.
    """
    return await _exec(
        api.feedback,
        project_dir=_resolve_project_dir(project_dir),
        ftype=type,
        title=title,
        body=body,
        severity=severity,
        expected=expected,
        got=got,
        why=why,
        proposal=proposal,
        stdout=stdout,
    )


# ─── Auto-commit approval ───────────────────────────────────────────────


async def awf_approve(
    todo_id: str,
    project_dir: str | None = None,
    *,
    evidence: str = "",
    verified_sha: str = "",
) -> dict[str, Any]:
    """Approve auto-commit for a TODO in --auto mode.

    Creates APPROVE-{todo_id}.ready signal. If the pipeline is waiting for
    approval, it will commit and continue. Idempotent — safe to call
    multiple times.

    Args:
        todo_id: TODO identifier to approve.
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        evidence: SPEC A-run — in run (забег) mode this is REQUIRED: the
            commands you actually ran and your verdict, e.g.
            "pytest -q → 348 passed; ruff → clean; diff checked; verdict: approve".
            Stored to .agentic/context/RUN-EVIDENCE-{todo}.md for owner audit.
        verified_sha: U11 (B5) — the working-tree fingerprint recorded at
            verify time (`awf tree-sha`). If passed, approve is REFUSED
            when the tree moved since verification (new commit, edited
            file, new untracked file). Without it the behavior is as
            before. On match the fingerprint is stored to
            .agentic/context/VERIFIED-{todo}.sha.

    Returns:
        Dict with: todo_id, signal_file (path to APPROVE-*.ready),
        verified_sha_file (when verified_sha matched).
    """
    try:
        pd = _resolve_project_dir(project_dir)
        result = api.approve_commit(
            pd, todo_id, evidence=evidence, verified_sha=verified_sha
        )
        response = _ok(result)
        # SMO: tell weak models to STOP calling approve (dogfood #4: 5x repeat).
        # AUD05-03: the old fixed text promised "approved and committed.
        # Pipeline exited." — approve only writes the signal; neither the
        # commit nor the exit is guaranteed by it. Build the hint from facts.
        # RUN10 #1: "run active" comes from the single source
        # (api.run_is_active → awf.run_state), not a duplicated run_brief probe.
        run_active = False
        try:
            run_active = bool(api.run_is_active(pd))
        except Exception:
            pass
        if run_active:
            response["next_action"] = (
                f"{todo_id} approved — APPROVE signal written"
                + (", evidence stored" if result.evidence_file else "")
                + ". Continue the run loop: awf_run_next."
            )
        else:
            pipeline_alive = False
            try:
                st = api.get_status(pd)
                pipeline_alive = bool(st and st.pipeline_running)
            except Exception:
                pipeline_alive = False
            if pipeline_alive:
                response["next_action"] = (
                    f"{todo_id} approved — APPROVE signal written. The pipeline "
                    "acts on it at its verify/commit gate; a commit happens only "
                    "if the stage policy auto-commits. Wait for the user before "
                    "the next TODO."
                )
            else:
                response["next_action"] = (
                    f"{todo_id} approved — APPROVE signal written, but the "
                    "pipeline is not running: the signal waits in the inbox for "
                    "the next run. Check awf_status. Wait for the user before "
                    "the next TODO."
                )
        # RUN5 #1 (Part B): failsafe — rejected-attempt files that would be
        # SILENTLY excluded from this commit. Warning only: the approve went
        # through, nothing is auto-committed.
        orphaned = list(getattr(result, "orphaned_files", None) or [])
        if orphaned:
            response["orphaned_files"] = orphaned
            response["warning"] = (
                f"{len(orphaned)} file(s) of a rejected attempt would NOT be "
                f"committed: {', '.join(orphaned)}. Re-issue the unit with "
                "carry_over_from=<rejected id>, or commit them consciously."
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

    Writes REVIEW-{todo_id}.md to outbox + creates signal. The engine owns
    the REVIEW transition itself: a live pipeline picks the signal up at its
    verify stage (replan → new TODO → stop), and a stopped pipeline picks it
    up on the next continue. No pipeline kill here (AUD08-04) — killing the
    waiting pipeline used to contradict this docstring and left the run in
    a non-deterministic state.

    Args:
        todo_id: TODO identifier to reject.
        reason: Why the work is rejected (what needs fixing).
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: todo_id, review_file, next_action.
    """
    # AUD08-13: the duplicate validation (todo_id regex, reason.strip()) that
    # lived here is removed — api.reject_commit validates the same way and
    # raises AwfApiError, which the except below turns into a clean error
    # dict. One validation layer (the API), like every other wrapper.
    try:
        pd = _resolve_project_dir(project_dir)
        result = api.reject_commit(pd, todo_id, reason)

        if result.run_stopped:
            next_action = (
                f"{todo_id} rejected twice — RUN STOPPED. Report: {result.report_file}. "
                "Notify the owner and wait for instructions."
            )
        else:
            next_action = (
                f"{result.message} The engine owns the REVIEW transition — do not "
                "kill the pipeline yourself. If it is alive at the verify stage it "
                "consumes the REVIEW, replans (new TODO) and stops. If it is stopped, "
                "the REVIEW stays in the outbox: the next awf_continue reports it, "
                "so dispatch the refined TODO and continue."
            )
        # RUN5 #1 (leak-gate): the rejected attempt's untracked files were
        # recorded — tell the supervisor how the retry keeps them.
        reject_files = list(getattr(result, "reject_files", None) or [])
        if reject_files:
            next_action += (
                f" When re-issuing the retry, pass carry_over_from={todo_id} — "
                f"{len(reject_files)} untracked file(s) of the rejected attempt "
                f"were recorded (REJECT-{todo_id}.files) and will join the "
                "retry's commit."
            )
        return {
            "status": "ok",
            "todo_id": todo_id,
            "review_file": result.review_file,
            "rejects": result.rejects,
            "run_stopped": result.run_stopped,
            "report_file": result.report_file,
            "reject_files": reject_files,
            "next_action": next_action,
        }
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── Reports ────────────────────────────────────────────────────────────


async def awf_report(project_dir: str | None = None) -> dict[str, Any]:
    """Generate workflow report — task statuses, git diff, latest test log.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

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
    """Clean runtime data — destructive, confirm with the user first.

    Modes (mutually exclusive):
    - ``tasks_only``: clean only inbox + outbox.
    - ``full``: clean inbox/outbox/context/logs + handoff/inputs/
      dashboards (iteration artifacts).
    - ``orphans``: remove orphan TODOs (active without progress).
    - default: clean inbox/outbox/context/logs (keep phases,
      handoff, inputs, dashboards).

    **Destructive** — clears runtime state. The ``orphans`` mode in CLI
    asks for confirmation; here we proceed (agent should ask user first
    via a form if confirmation is needed).

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        tasks_only: Clean only inbox + outbox (default: False).
        full: default clean + handoff/inputs/dashboards (default: False).
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
    name: str = "",
    project_dir: str | None = None,
    *,
    description: str = "",
    model: str = "",
    from_skill: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Generate a new role file at .agentic/roles/{name}.md.

    By default creates a placeholder role file with sections for
    responsibility, input, actions, output, and prohibitions. With
    ``from_skill`` the role is created from an opencode skill instead:
    the SKILL.md body (its own YAML front-matter stripped) under a
    provenance comment. Skill search: project ``.opencode/skills/<name>/``
    first, then global ``~/.config/opencode/skills/<name>/``. An unknown
    skill is an error listing the available skills.

    Args:
        name: Role slug (e.g. "qa", "reviewer", "auditor"). With
            ``from_skill`` and empty ``name``, the skill name is used.
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        description: One-line role description (template mode only).
        model: Model ID for this role (template mode only). If empty,
            placeholder inserted.
        from_skill: Skill slug to copy the role content from (e.g.
            "agent-security-auditor").
        force: Overwrite the role file if it already exists (default:
            refuse — the file is user data).

    Returns:
        Dict with: role_name, role_file (path), model.
    """
    try:
        result = api.add_role(
            _resolve_project_dir(project_dir),
            name,
            description=description,
            model=model,
            from_skill=from_skill,
            force=force,
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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
        # AUD08-06: next_action is built from the ACTUAL phase. The old
        # unconditional "call awf_confirm_normalized" steered a weak model
        # into a phase jump from any other phase (advance_phase steps from
        # the current one: run → verify).
        try:
            from awf.phase import detect_phase

            phase = detect_phase(_resolve_project_dir(project_dir))
        except Exception:
            phase = None
        if phase == "normalize":
            response["next_action"] = (
                "Roles analyzed. Call awf_confirm_normalized to advance to "
                "brief phase."
            )
        elif phase:
            response["next_action"] = (
                f"Roles analyzed (current phase: {phase}). No phase change "
                "needed — do NOT call awf_confirm_normalized outside the "
                "normalize phase. Continue with the current phase."
            )
        else:
            response["next_action"] = (
                "Roles analyzed. Check awf_current_step for the phase and "
                "the next step."
            )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


# ─── RUN3 #1: named pipelines (create + list) ────────────────────────────


async def awf_write_pipeline(
    name: str,
    stages: list[dict[str, Any]],
    project_dir: str | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Write a named pipeline file at .agentic/pipelines/<name>.yaml.

    Side-effect contract: ONLY the pipeline file is written — config.yaml
    and supervisor.md are NOT touched (unlike the project-setup form,
    which patches them). An existing pipeline is refused without
    ``force=True``. Run it afterwards with ``awf_start(pipeline=name)``
    or CLI ``awf start --pipeline <name>``.

    Stage schema (each item in ``stages`` — the pipeline YAML keys):
    ``role`` (required), ``name`` (defaults to the role slug),
    ``description``, ``on_blocked`` / ``on_approved`` / ``on_rejected`` /
    ``on_failed``, ``max_retries``, ``max_rollbacks``. Unknown keys are
    refused (the loader would ignore them).

    Args:
        name: Pipeline name — letters, digits, '_', '.', '-' (no path
            separators or spaces), e.g. "audit-llm".
        stages: Non-empty list of stage objects (pipeline YAML schema).
            Typically: plan(supervisor) → worker stages → verify(supervisor).
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        force: Overwrite an existing pipeline file (default: False).

    Returns:
        Dict with: name, file (path), stages (count written), overwritten.
        On error: {status: "error", error: "..."}.
    """
    try:
        result = api.write_pipeline(
            _resolve_project_dir(project_dir),
            name,
            stages,
            force=force,
        )
        response = _ok(result)
        response["next_action"] = (
            f"Pipeline '{result.name}' written ({result.stages} stages). "
            f"Run it: awf_start(project_dir, pipeline='{result.name}')."
        )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}


async def awf_pipelines(project_dir: str | None = None) -> dict[str, Any]:
    """List the pipeline files in .agentic/pipelines/ + the active one.

    The active pipeline is ``default_pipeline`` from config.yaml
    ("default" when undeclared) — the file the engine uses when no
    explicit name is given.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: pipelines (sorted names), active, active_exists.
        On error: {status: "error", error: "..."}.
    """
    try:
        result = api.list_pipelines(_resolve_project_dir(project_dir))
        return _ok(result)
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
    pipeline: str | None = None,
    carry_over_from: str | None = None,
    include_untracked: list[str] | None = None,
) -> dict[str, Any]:
    """Create a unit atomically: TODO file + baseline + .ready signal in one call.

    Replaces the manual 3-step workflow (write md → awf_baseline → touch
    .ready). One call = TODO ready to be picked up by next pipeline run.

    Auto-picks next NNNN by scanning inbox + outbox (avoids collisions
    with already-completed TODOs). Creates BASELINE-NNNN.{sha,status,
    tests.log,env.log} snapshot. Writes TODO-NNNN.ready signal.

    Args:
        content: TODO markdown body (the task description, Mode A/B/C
            per supervisor.md).
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        role: Optional role hint for debugging. Stored as HTML comment
            in TODO .md. Does NOT affect pipeline routing (that's
            determined by stage order in pipeline.yaml).
        todo_id: Override auto-generated id (e.g. "TODO-0007"). Must
            match pattern TODO-NNNN.
        pipeline: Optional pipeline name (RUN3 #2) — written into the TODO's
            front-matter as ``pipeline: <name>``. When awf_run_next launches
            this TODO without a queue-level pipeline, it uses this one.
        carry_over_from: RUN5 #1 (leak-gate) — id of a REJECTED TODO whose
            untracked files this retry re-claims. Its
            ``.agentic/context/REJECT-<origin>.files`` paths are excluded
            from the new baseline's untracked snapshot, so the retry's
            commit includes them. Use this when re-issuing a rejected unit.
            Refused (no side effects) when the origin TODO or its REJECT
            file is missing.
        include_untracked: RUN10 #4 — project-relative paths of pre-existing
            untracked files to include in this unit's commit (e.g.
            ["docs/notes.md"]). The commit gate otherwise excludes files
            that were untracked BEFORE the dispatch; with this parameter
            they join the unit commit. Each path must exist, be untracked,
            not gitignored, and stay inside the project — any invalid path
            refuses the dispatch (no side effects). Traced in
            ``.agentic/context/BASELINE-<id>.include``. The answer ALSO
            lists the files that stay excluded (``pre_existing_untracked``
            + ``untracked_warning``).

    Returns:
        Dict with: todo_id, baseline_sha, role_hint, files_written (list
        of paths created), carry_over_from, carry_over_files,
        pre_existing_untracked (list, excluded from the commit),
        untracked_warning (one line, "" when nothing is excluded).
    """
    try:
        result = api.dispatch_todo(
            _resolve_project_dir(project_dir),
            content,
            role=role,
            todo_id=todo_id,
            pipeline=pipeline,
            carry_over_from=carry_over_from,
            include_untracked=include_untracked,
        )
        response = _ok(result)
        # SMO: next_action + pre-check warnings guide weak models
        warnings = getattr(result, "pre_check_warnings", None) or []
        if warnings:
            response["pre_check_warnings"] = warnings
            response["next_action"] = (
                f"{result.todo_id} dispatched. ⚠️ Pre-check: "
                f"{len(warnings)} pattern(s) already in code. "
                "Verify task is needed BEFORE calling awf_start."
            )
        else:
            response["next_action"] = (
                f"{result.todo_id} dispatched. Call awf_start(background=True) to launch pipeline."
            )
        if getattr(result, "carry_over_files", None):
            response["next_action"] = (
                f"{result.todo_id} dispatched with carry-over from "
                f"{result.carry_over_from}: {len(result.carry_over_files)} "
                "file(s) of the rejected attempt will join the retry commit. "
                "Call awf_start(background=True) to launch pipeline."
            )
        # RUN10 #4 (TODO-0074): the excluded pre-existing untracked files
        # must not be invisible — the warning reaches next_action.
        untracked_warning = getattr(result, "untracked_warning", "") or ""
        if untracked_warning:
            response["next_action"] = (
                f"{response['next_action']} {untracked_warning}"
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

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
            "brief": "Write TODO-NNNN.md (awf_dispatch_todo) → .ready signal.",
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
    # AUD08-10: docstring says "2-5 variant dicts" but the code accepted 1
    # (an anti-interface — a "choice" form with a single card) and up to 6
    # (the error message itself said 2-5). Validation now matches the
    # docstring: exactly 2..5.
    if len(variants) > 5:
        return {
            "status": "error",
            "error": f"too many variants ({len(variants)}) — keep to 2-5 for user clarity",
        }
    if len(variants) < 2:
        return {
            "status": "error",
            "error": "at least 2 variants are required — a single card is not a choice",
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


def _port_alive(port: int, timeout: float = 0.3) -> bool:
    """AUD10-05: is anyone actually listening on the dashboard port?

    The port file survives process death — a plain file check opens a dead
    URL after every finished/killed run.
    """
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _open_dashboard_sync(pd: Path, wait: bool = False) -> dict[str, Any]:
    """Open dashboard in browser — single implementation (AUD08-05).

    Prefers the live HTTP server (polling + liveness-checked port), falls
    back to a generated file:// page. Synchronous on purpose: every caller
    runs it through ``asyncio.to_thread`` — a ``time.sleep`` inside an async
    body froze the whole MCP event loop for up to 5 s per background start.

    ``wait=True`` (just launched the pipeline): poll the port file up to
    ~5 s while the orchestrator boots its server. ``wait=False`` (standalone
    open): one immediate check, no sleeping.
    """
    import time
    import webbrowser

    port_file = pd / ".agentic" / "state" / "dashboard_port"
    attempts = 10 if wait else 1
    for attempt in range(attempts):
        if port_file.is_file():
            try:
                port = int(port_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                port = 0
            if port and _port_alive(port):
                url = f"http://127.0.0.1:{port}"
                webbrowser.open(url)
                return {"opened": True, "url": url, "method": "http"}
        if wait and attempt < attempts - 1:
            time.sleep(0.5)

    # Fallback: file://
    from awf.api.dashboard import generate_dashboard

    generate_dashboard(pd)
    dashboard_path = pd / ".agentic" / "dashboards" / "current.html"
    if dashboard_path.is_file():
        webbrowser.open(f"file://{dashboard_path}")
        return {"opened": True, "url": f"file://{dashboard_path}", "method": "file"}
    return {"opened": False, "url": "", "method": "none"}


async def _open_dashboard_browser(pd: Path) -> dict[str, Any]:
    """Async facade over :func:`_open_dashboard_sync` (AUD08-05: one impl,
    executed off the event loop)."""
    return await asyncio.to_thread(_open_dashboard_sync, pd, False)


async def awf_open_pipeline_dashboard(
    project_dir: str,
) -> dict[str, Any]:
    """Open pipeline dashboard in user's browser.

    Prefers HTTP server (live updates via /api/state polling).
    Falls back to file:// if server unavailable.

    Args:
        project_dir: **Required.** Absolute path to awf project root.

    Returns:
        Dict with: status, opened (bool), url, method.
    """
    p = _resolve_project_dir(project_dir)
    result = await _open_dashboard_browser(p)
    # AUD08-11: the module contract is that every status:"error" carries an
    # error message — the old code returned an error dict without one, so
    # the model could not tell "no dashboard" from "server not up".
    if result["opened"]:
        return {
            "status": "ok",
            "opened": True,
            "url": result["url"],
            "method": result["method"],
        }
    return {
        "status": "error",
        "opened": False,
        "url": "",
        "method": "none",
        "error": (
            "Dashboard could not be opened: no live dashboard server port "
            "(.agentic/state/dashboard_port) and no generated "
            ".agentic/dashboards/current.html. Start the pipeline first "
            "(awf_start) or generate the dashboard, then retry."
        ),
    }


# ─── DASH Phase 3: supervisor wake-up (no more polling) ─────────────────

# Wrapper-side cap for a single wait. The ACTUAL cap is lower and
# project-aware (RUN6 #3): the MCP client transport (default ~60s timeout)
# cuts a wait at ~55s (B3, run2 report) — awf.api.wait_cap() resolves it
# (env AWF_WAIT_CAP / config wait.cap_seconds, default TRANSPORT_CAP=55).
# next_action says so on every response.
MAX_WAIT = 600


def _wait_cap_note(project_dir, clamped: bool, requested: int) -> str:
    """B3 / RUN6 #3 / RUN10 #2: append the ACTUAL single-wait cap to
    every next_action.

    The supervisor used to wait with timeout=180 and get the wait cut at
    ~55s, then hammer retries. The note makes the working cap explicit;
    when the request was clamped to MAX_WAIT it says so.

    RUN6 #3: the cap is project-aware (wait_cap: env AWF_WAIT_CAP / config
    wait.cap_seconds, default TRANSPORT_CAP).

    RUN10 #2: while the cap is the default the note names the EXACT tool
    setting with the CONCRETE value (api.cap_advice reads the mcp timeout
    from opencode.json: T = timeout ms / 1000 - 30). The "raise the mcp
    timeout in opencode.json" clause appears ONLY when that timeout is
    unknown or below the cap — never when the cap is no longer the
    default (the owner already raised the ceiling, the advice is stale).
    """
    try:
        cap = api.wait_cap(project_dir)
    except Exception:
        cap = api.TRANSPORT_CAP
    if cap == api.TRANSPORT_CAP:
        try:
            advice = api.cap_advice(project_dir)
        except Exception:
            advice = (
                f"{cap}s is the tool's own cap, not the transport — to "
                "wait longer set wait.cap_seconds in .agentic/config.yaml "
                "or AWF_WAIT_CAP — wait in smaller steps"
            )
        note = f" Single wait <= {cap}s ({advice})."
    else:
        note = (
            f" Single wait <= {cap}s (project wait cap: wait.cap_seconds "
            f"config or AWF_WAIT_CAP env; wrapper cap {MAX_WAIT}s)."
        )
    if clamped:
        note = f" Requested {requested}s was clamped to {MAX_WAIT}s." + note
    return note


async def awf_wait_for_event(
    project_dir: str | None = None,
    timeout: int = 30,
    actionable_only: bool = False,
) -> dict[str, Any]:
    """Check for pipeline events (reactive, NOT for proactive polling).

    R6 sleep mode: Do NOT call this in a loop after ``awf_start``.
    Instead: open dashboard → go idle → respond to user messages.
    Use this tool ONLY when the user writes you about a pipeline event
    (e.g. "pipeline finished", "salvage", "blocked") to get structured
    event details before acting.

    SPEC A-run (забег): inside an active run the supervisor DOES wait in a
    loop. Call it with ``actionable_only=True`` and ``timeout`` from the
    previous result's ``suggested_timeout`` (awf sizes it from measured stage
    durations — sleep in chunks instead of hammering every 30s).

    Returns immediately when:

    - ``verify`` — pipeline reached verify stage (supervisor must act)
    - ``blocked`` — worker wrote BLOCKED signal
    - ``salvage`` — worker died without a signal (highest priority)
    - ``checkpoint`` — BD-36 checkpoint form opened (tell user)
    - ``done`` — pipeline cycle complete (after approve: the TODO is
      committed + archived, stage state cleared); the message names the
      TODO and the next command — ``awf_run_next`` in a run,
      ``awf_dispatch_todo`` outside
    - ``timeout`` — no event within timeout
    - ``stage_changed`` — stage transition (suppressed by actionable_only)

    R3 (NEG-2026-09-19): the MCP transport cuts long tool calls (JSON-RPC
    -32001) — the default client timeout is ~60s, so a single wait works
    up to ~55s (B3, run2 report). This wrapper clamps the wait to MAX_WAIT
    (600s). The single-wait cap is project-aware (RUN6 #3): env
    ``AWF_WAIT_CAP`` > ``.agentic/config.yaml`` ``wait.cap_seconds`` > the
    default 55s.

    RUN10 #2 (honest advice): the 55s default is the tool's OWN cap, not
    the transport — a raised mcp timeout in opencode.json does not lift
    it. ``next_action`` of EVERY response names the exact lever: while
    the cap is the default it says to set ``wait.cap_seconds: <T>`` in
    ``.agentic/config.yaml`` or ``AWF_WAIT_CAP=<T>`` (T = the
    ``agent-workflow-ui`` mcp timeout from opencode.json minus ~30s, when
    readable; without the number when it is not). The "raise the mcp
    timeout in opencode.json" clause appears ONLY when that timeout is
    unknown or below the cap — and NEVER once the cap is raised in
    config/env. ``suggested_timeout`` with no stage history follows the
    actual cap (~90% of it: 55 → 55, 600 → 540), not the 55s default.
    When ``timeout_clamped`` is true the note comes with the clamp fact.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        timeout: Max seconds to block (default 30 — reactive mode; clamped
            to 600). In a run loop pass suggested_timeout: the call runs in
            a worker thread, long waits do NOT freeze other MCP tools.
        actionable_only: Only events needing supervisor action end the wait.

    Returns:
        Dict with: event_type (verify/blocked/salvage/checkpoint/done/
        timeout/stage_changed/idle), message (instruction for supervisor),
        state_snapshot, suggested_timeout (recommended wait size for the
        next call, never above the project's wait cap), next_action
        (instruction + the actual single-wait cap), timeout_clamped (true
        when the requested timeout exceeded the wrapper cap).
    """
    # AUD08-07: a non-numeric timeout used to raise ValueError OUTSIDE the
    # try below — the module contract is "never escape with an exception".
    try:
        requested = int(timeout or 0)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "error": f"timeout must be an integer number of seconds, got {timeout!r}",
        }
    timeout = max(1, min(requested, MAX_WAIT))
    clamped = requested > MAX_WAIT
    try:
        result = await asyncio.to_thread(
            api.wait_for_event,
            _resolve_project_dir(project_dir),
            timeout=timeout,
            actionable_only=actionable_only,
        )
        response = _ok(result)
        if clamped:
            response["timeout_clamped"] = True
            response["timeout_requested"] = requested
        # AUD08-01: wait_for_event returns a WaitEventResult dataclass, not a
        # dict — the old isinstance(result, dict) check was always False, so
        # next_action was stuck on the timeout instruction. _ok() already
        # carries event_type via as_dict().
        et = response.get("event_type") or "timeout"
        # RUN6 #3: the fallback cap is project-aware (wait.cap_seconds /
        # AWF_WAIT_CAP), not the hardcoded transport default. RUN10 #2:
        # the no-history fallback follows the cap (~90% of it), not the
        # 55s transport default.
        try:
            cap = api.wait_cap(_resolve_project_dir(project_dir))
        except Exception:
            cap = api.TRANSPORT_CAP
        suggested = response.get("suggested_timeout") or (
            api.default_suggested_timeout(cap)
        )

        # SPEC A-run: inside an active run the supervisor keeps waiting;
        # outside it stays idle (R6 reactive mode).
        # RUN10 #1: the "run active" decision comes from the single source
        # (awf.run_state.run_is_active, exported as api.run_is_active) —
        # the old run_brief probe was a duplicated, heavier check.
        run_active = False
        try:
            run_active = bool(api.run_is_active(_resolve_project_dir(project_dir)))
        except Exception:
            pass

        if run_active:
            _EVENT_ACTIONS = {
                "verify": "Pipeline at verify. Run your own probes → awf_approve(evidence=...) or awf_reject.",
                "blocked": "Worker blocked. Read BLOCKED note → fix context and awf_retry_stage, or stop with awf_run_finish.",
                "checkpoint": "Checkpoint form opened. Tell the user (the run is paused until they submit).",
                "salvage": "Salvage needed. Read SALVAGE note → awf_retry_stage / ACK / split the TODO.",
                "done": (
                    "Cycle complete. Next step: awf_run_next(project_dir) — "
                    "launch the next queued TODO (or stop at a gate). If the "
                    "cycle archived nothing, check awf_status first."
                ),
                "stage_changed": (
                    f"Stage changed — no action needed. Keep waiting: "
                    f"awf_wait_for_event(timeout={suggested}, actionable_only=True)."
                ),
                "timeout": (
                    f"No event yet. Continue the run loop: "
                    f"awf_wait_for_event(timeout={suggested}, actionable_only=True)."
                ),
                "idle": "Pipeline not running. Check awf_status → awf_run_next or awf_run_finish.",
            }
        else:
            _EVENT_ACTIONS = {
                "verify": "Pipeline at verify. Read handoffs + git diff → awf_approve.",
                "blocked": "Worker blocked. Read BLOCKED note → replan or adjust TODO.",
                "checkpoint": "Checkpoint form opened in browser. Tell user to approve.",
                "done": (
                    "Pipeline complete. Next step: awf_dispatch_todo(project_dir, "
                    "content) for the next task, or awf_status to review."
                ),
                "salvage": "Salvage needed. Read SALVAGE note → awf_retry_stage or ACK.",
                "timeout": "No event. DO NOT call awf_wait_for_event again. Wait for user.",
            }
        # RUN10 #1: an UNKNOWN event type inside a run must not advise
        # "wait for user" either — the run loop keeps waiting.
        default_action = (
            f"Check awf_status, then continue the run loop: "
            f"awf_wait_for_event(timeout={suggested}, actionable_only=True)."
            if run_active
            else "Check awf_status, then wait for user."
        )
        response["next_action"] = _EVENT_ACTIONS.get(et, default_action)
        # B3 / RUN6 #3: every response carries the actual single-wait cap
        # (project-aware: config/env, default ~55s transport cap); clamped
        # requests say so explicitly.
        response["next_action"] += _wait_cap_note(
            _resolve_project_dir(project_dir), clamped, requested
        )
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: models (list of {role, model, provider, valid, note}),
        warnings (list of strings), providers_available (list).
    """
    try:
        result = await asyncio.to_thread(
            api.check_model_config, _resolve_project_dir(project_dir)
        )
        response = {"status": "ok", **result}
        if result.get("warnings"):
            response["next_action"] = (
                "Model config has warnings — fix them in .agentic/config.yaml "
                "(models/providers), then re-run awf_check_model_config before awf_start."
            )
        else:
            response["next_action"] = (
                "Models valid — proceed: awf_dispatch_todo for the next unit, "
                "then awf_start."
            )
        return response
    except api.AwfApiError as e:
        return _err(e)
    except Exception as e:
        return {"status": "error", "error": f"Unexpected {type(e).__name__}: {e}"}

# ─── Kill pipeline (SELF-2) ─────────────────────────────────────────────


async def awf_kill(
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Kill the running pipeline AND its stage worker.

    Reads pipeline_pid from state, sends SIGTERM, waits 5s, SIGKILL if
    still alive, clears state — and kills the stage worker's process group
    the same way (RUN8 #2: a pipeline-only kill left the orphaned worker
    writing the unit's DONE after the stop). The answer names the pipeline
    and worker pids; a worker that survives is called out by pid. Use when
    the pipeline is stuck or needs to stop.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: killed (bool), pid, workers ({pid: status}), message;
        reason ("ancestry") on refusal.
    """
    try:
        result = await asyncio.to_thread(
            api.kill_pipeline, _resolve_project_dir(project_dir)
        )
        response = {"status": "ok", **result}
        if result.get("killed"):
            worker_flags = [
                f"PID {wpid} ({status})"
                for wpid, status in (result.get("workers") or {}).items()
                if status in ("survived", "unverified")
            ]
            if worker_flags:
                response["next_action"] = (
                    "Pipeline stopped, but a worker survived — "
                    + ", ".join(worker_flags)
                    + ". Stop it manually (kill), check its edits, then "
                    "resume with awf_continue."
                )
            else:
                response["next_action"] = (
                    "Pipeline stopped. Check awf_status — resume with "
                    "awf_continue, or replan (awf_dispatch_todo) if the unit "
                    "needs new shape."
                )
        else:
            if result.get("reason") == "ancestry":
                response["next_action"] = (
                    "Kill refused: this process is inside the pipeline it "
                    "is trying to kill. Run awf_kill from the supervisor "
                    "session, not from inside the pipeline."
                )
            else:
                response["next_action"] = (
                    "No pipeline was running (nothing to stop) — start one: "
                    "awf_start(project_dir), or awf_run_next inside a run."
                )
        return response
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

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


async def awf_brief(project_dir: str | None = None) -> dict[str, Any]:
    """Show the live supervisor onboarding/recovery card, assembled from project state.

    Call this at the START of a new session (or when context is lost)
    instead of re-reading files: header (awf version, project, phase,
    date), what's next, state (run, active tasks, blocked/salvage, last
    signal), defaults, the five scenarios, the tool map by situation,
    rituals, recovery recipes, what's new (latest CHANGELOG section),
    feedback line (RUN4 #1, expanded in RUN6 #5).

    Not a static document — the card is built from live project state so
    it does not go stale. An empty, new, or nonexistent project does not
    fail: the card degrades to header + setup-chain hint + tool map.
    Deterministic for the same state (identical text except the date line).

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: status ("ok"), version, project, phase, date,
        is_live_project, next_action, run, active_todos, blocked,
        salvage_stage, last_signal, pipeline_running, tool_map (groups),
        rituals, recovery, doctrine, what_new, text (the rendered card).
        On error: {status: "error", error: "..."}.
    """
    return await _exec(api.brief, project_dir=_resolve_project_dir(project_dir))


async def awf_set_goal(
    goal: str,
    project_dir: str | None = None,
) -> dict[str, Any]:
    """Set session goal and advance to form phase.

    SMO.3: Called after supervisor elicited goal from user. Stores goal
    in state and advances phase: goal → form.

    Args:
        goal: User's goal for this session (1-3 sentences).
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: phase ("form"), goal (str).
    """
    try:
        from awf.phase import advance_phase

        pd = _resolve_project_dir(project_dir)
        # AUD02-05: declare the documented transition — set_goal works only
        # from the goal phase, otherwise it refuses (mid-run phase flips
        # were corrupting the phase key).
        new_phase = await asyncio.to_thread(
            advance_phase, pd, from_phase="goal", goal=goal
        )
        # SMO: next_action guides weak models — don't let them guess
        _NEXT = {
            "form": "Открой project-setup форму через awf_open_project_setup_form.",
            "normalize": "Выполни normalize checklist, затем awf_confirm_normalized.",
            "brief": "Изучи vision и план, напиши TODO-NNNN.md (awf_dispatch_todo) и запусти пайплайн.",
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

    Returns:
        Dict with: phase ("brief").
    """
    try:
        from awf.phase import advance_phase

        pd = _resolve_project_dir(project_dir)
        # AUD02-05: declare the documented transition (normalize → brief).
        new_phase = await asyncio.to_thread(
            advance_phase, pd, from_phase="normalize", normalized=True
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
