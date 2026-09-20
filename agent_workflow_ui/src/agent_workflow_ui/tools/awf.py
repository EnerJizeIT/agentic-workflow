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
    todo_id: str = "",
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
    ack: str = "",
    background: bool = True,
) -> dict[str, Any]:
    """Resume an interrupted pipeline. Finds newest active TODO and continues.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        pipeline: Pipeline name to run (default: from config.yaml).
        from_stage: Start from a specific stage name.
        auto: Skip interactive supervisor waits (CI mode).
        timeout: Agent stage timeout in seconds (default: 3600).
        ack: Accept a BLOCKED TODO and resume (e.g. "TODO-0009"). Writes
            ACK-{todo}.ready and clears the BLOCKED closure — the supervisor's
            answer survives process death. Without it, a pending ACK/APPROVE
            is still picked up; a blocked TODO with no answer returns a clear
            instruction instead of "No active TODO found".
        background: Detach and return immediately (default: True, the API
            default). Set ``background=False`` to block the call until the
            pipeline finishes (AUD08-02 — foreground continue was previously
            impossible from MCP).

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
        # AUD08-02: signature parity with api.continue_pipeline.
        background=background,
    )
    if isinstance(result, dict) and result.get("run_mode") == "background":
        # Open dashboard (same as awf_start)
        try:
            import time as _time
            pd = _resolve_project_dir(project_dir)
            _time.sleep(1)  # wait for server to start
            dash = await _open_dashboard_browser(pd)
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
    cleans salvage signals, restarts from that stage.

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
        try:
            import time as _time
            pd = _resolve_project_dir(project_dir)
            _time.sleep(1)
            dash = await _open_dashboard_browser(pd)
            result["dashboard_opened"] = dash["opened"]
        except Exception:
            pass
        result["next_action"] = "Stage retried. GO IDLE — wait for user."
    return result


# ─── Autonomous run (забег) — SPEC A-run v1 ─────────────────────────────


async def awf_run_start(
    project_dir: str | None = None,
    *,
    queue: list[str] | None = None,
    budget_minutes: int = 0,
    stop_flags_json: str = "",
    note: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Start an autonomous run: a queue of TODOs with mechanical gates.

    The supervisor chat drives the loop; awf keeps the state and enforces
    the stop-list (queue/budget/stop-flags/rejects). The owner is pinged
    only on stop conditions or at the end.

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        queue: Ordered TODO ids to run, e.g. ["TODO-0010", "TODO-0011"].
        budget_minutes: Optional time budget (0 = unlimited).
        stop_flags_json: Optional JSON map of TODO id → [reason], e.g.
            '{"TODO-0012": ["phase-boundary", "external-audit"]}'. awf refuses
            to auto-continue past a flagged item — the run stops there.
        note: R5 — one-line "what is happening now" for the owner's dashboard.
            Keep it fresh with awf_run_note on every stage change.
        force: Replace an already-active run state (recovery from a stale or
            wrong-directory run). Without it a second run_start is refused.

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
    """R5: set the run's live description ("что сейчас делается").

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
    return await _exec(
        api.restore_todo,
        project_dir=_resolve_project_dir(project_dir),
        todo_id=todo_id,
    )


async def awf_run_status(project_dir: str | None = None) -> dict[str, Any]:
    """Current run (забег) state: position, budget left, rejects, stop reason."""
    return await _exec(api.run_status, project_dir=_resolve_project_dir(project_dir))


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
        try:
            import time as _time
            pd = _resolve_project_dir(project_dir)
            _time.sleep(1)
            dash = await _open_dashboard_browser(pd)
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
    return await _exec(
        api.run_finish,
        project_dir=_resolve_project_dir(project_dir),
        reason=reason,
        summary=summary,
    )


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
        return _ok(result)
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
    """U4: machine-proof that declared tests are red on the baseline sha.

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
    """U5: one deterministic verify report for the supervisor.

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


# ─── Auto-commit approval ───────────────────────────────────────────────


async def awf_approve(
    todo_id: str,
    project_dir: str | None = None,
    *,
    evidence: str = "",
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

    Returns:
        Dict with: todo_id, signal_file (path to APPROVE-*.ready).
    """
    try:
        result = api.approve_commit(
            _resolve_project_dir(project_dir), todo_id, evidence=evidence
        )
        response = _ok(result)
        # SMO: tell weak models to STOP calling approve (dogfood #4: 5x repeat)
        response["next_action"] = (
            f"{todo_id} approved and committed. Pipeline exited. "
            "Wait for user to decide next step. "
            "DO NOT dispatch next TODO without user asking."
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
        return {
            "status": "ok",
            "todo_id": todo_id,
            "review_file": result.review_file,
            "rejects": result.rejects,
            "run_stopped": result.run_stopped,
            "report_file": result.report_file,
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
    """Clean runtime data.

    Modes (mutually exclusive):
    - ``tasks_only``: clean only inbox + outbox.
    - ``full``: clean inbox/outbox/context/logs/reports + handoff/inputs/
      dashboards (iteration artifacts).
    - ``orphans``: remove orphan TODOs (active without progress).
    - default: clean inbox/outbox/context/logs/reports (keep phases,
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
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


async def _open_dashboard_browser(pd: Path) -> dict[str, Any]:
    """Open dashboard in browser — prefers HTTP server, falls back to file://."""
    import webbrowser

    # Try HTTP server first (live polling, no reload)
    port_file = pd / ".agentic" / "state" / "dashboard_port"
    if port_file.is_file():
        try:
            port = int(port_file.read_text().strip())
            url = f"http://127.0.0.1:{port}"
            webbrowser.open(url)
            return {"opened": True, "url": url, "method": "http"}
        except (ValueError, OSError):
            pass

    # Fallback: file://
    from awf.api.dashboard import generate_dashboard
    generate_dashboard(pd)
    dashboard_path = pd / ".agentic" / "dashboards" / "current.html"
    if dashboard_path.is_file():
        webbrowser.open(f"file://{dashboard_path}")
        return {"opened": True, "url": f"file://{dashboard_path}", "method": "file"}
    return {"opened": False, "url": "", "method": "none"}


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
    - ``checkpoint`` — BD-36 checkpoint form opened (tell user)
    - ``done`` — pipeline completed (state file cleared)
    - ``timeout`` — no event within timeout
    - ``stage_changed`` — stage transition (suppressed by actionable_only)

    R3 (NEG-2026-09-19): the MCP transport cuts long tool calls (JSON-RPC
    -32001) — the default client timeout is ~60s. This wrapper clamps the
    wait to MAX_WAIT (600s). For longer single waits set the MCP server
    timeout in opencode.json::

        "mcp": {"agent-workflow-ui": {..., "timeout": 600000}}

    Args:
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).
        timeout: Max seconds to block (default 30 — reactive mode; clamped
            to 600). In a run loop pass suggested_timeout: the call runs in
            a worker thread, long waits do NOT freeze other MCP tools.
        actionable_only: Only events needing supervisor action end the wait.

    Returns:
        Dict with: event_type (verify/blocked/checkpoint/done/timeout/idle),
        message (instruction for supervisor), state_snapshot,
        suggested_timeout (recommended wait size for the next call),
        timeout_clamped (true when the requested timeout exceeded the cap).
    """
    MAX_WAIT = 600
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
        suggested = response.get("suggested_timeout") or 180

        # SPEC A-run: inside an active run the supervisor keeps waiting;
        # outside it stays idle (R6 reactive mode).
        run_active = False
        try:
            brief = api.run_brief(_resolve_project_dir(project_dir))
            run_active = bool(brief and brief.get("active"))
        except Exception:
            pass

        if run_active:
            _EVENT_ACTIONS = {
                "verify": "Pipeline at verify. Run your own probes → awf_approve(evidence=...) or awf_reject.",
                "blocked": "Worker blocked. Read BLOCKED note → fix context and awf_retry_stage, or stop with awf_run_finish.",
                "checkpoint": "Checkpoint form opened. Tell the user (the run is paused until they submit).",
                "salvage": "Salvage needed. Read SALVAGE note → awf_retry_stage / ACK / split the TODO.",
                "done": "Pipeline exited. Approved iteration → awf_run_next; failure → handle per the report.",
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
        project_dir: Project root. Default is the MCP process cwd ($HOME) —
            NOT your project; always pass it explicitly (AUD08-12).

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
        new_phase = await asyncio.to_thread(
            advance_phase, pd, goal=goal
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
