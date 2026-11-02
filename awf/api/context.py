"""Helpers of the supervisor status: stage info and expected action.

The one-shot bootstrap aggregate (``load_supervisor_context``) is gone —
``awf_brief`` is the single entry point. These helpers stay because
``get_status`` imports them (AC47).
"""
from __future__ import annotations

from pathlib import Path

from .. import paths
from ..pipeline import Stage, load_stages, resolve_pipeline_file
from ..signals import is_review_decision


def _load_pipeline_stages(project_dir: Path) -> list[Stage]:
    """AUD-3: resolve pipeline via canonical path (not hardcoded default.yaml)."""
    try:
        pipeline_file = resolve_pipeline_file(project_dir)
        return load_stages(pipeline_file)
    except Exception:
        return []


def _extract_stage_info(
    project_dir: Path,
) -> tuple[str | None, str | None, str | None, str | None, bool, int | None, str | None]:
    """Read pipeline stage info from the structured state file.

    T4.1: orchestrator + plan_checkpoint persist structured state to
    ``.agentic/state/current.yaml`` after each transition. This function
    reads that file — single source of truth (the regex fallback was
    removed in AUD-12.5; missing/stale state degrades to None values).

    AUD02-12: a stale ``updated_at`` does NOT disqualify the state while
    the pipeline PID is alive — one stage longer than the 2h window is a
    long stage, not a crash (see ``pipeline_state.state_trusted``).

    Returns ``(current_stage_name, next_stage_role, last_signal, log_tail,
    checkpoint_pending, checkpoint_port, checkpoint_form_url)``.
    """
    # ── T4.1: structured state file (single source of truth) ───────────
    from ..pipeline_state import read_state, state_trusted

    state = read_state(project_dir)
    log_tail_text = _read_log_tail(
        paths.agentic_dir(project_dir) / "logs" / "awf-start.out",
        30,
        markers=_run_banners(),
    )
    if state and state_trusted(project_dir, state):
        # All fields available from structured state — no regex needed.
        current_stage = state.get("stage_name")
        checkpoint_pending = bool(state.get("checkpoint_pending", False))
        checkpoint_port = state.get("checkpoint_port")
        checkpoint_form_url = state.get("checkpoint_form_url")
        last_signal = state.get("last_signal")

        # next_stage_role still needs pipeline.yaml lookup
        next_stage_role = _lookup_next_stage_role(project_dir, current_stage)

        # last_signal from state (None if missing — no regex fallback)
        return (
            current_stage,
            next_stage_role,
            last_signal,
            log_tail_text,
            checkpoint_pending,
            checkpoint_port,
            checkpoint_form_url,
        )

    # AUD-12.5: regex fallback removed. State file is always written since T4.1.
    # If state is missing (crash), supervisor gets None values and checks awf_status.
    next_stage_role = _lookup_next_stage_role(project_dir, None)
    return (None, next_stage_role, None, log_tail_text, False, None, None)


def _lookup_next_stage_role(project_dir: Path, current_stage: str | None) -> str | None:
    """Look up next stage role from pipeline.yaml based on current stage."""
    stages = _load_pipeline_stages(project_dir)
    if not stages:
        return None
    role_by_name = {s.name: s.role for s in stages}
    if current_stage and current_stage in role_by_name:
        names = list(role_by_name.keys())
        idx = names.index(current_stage)
        if idx + 1 < len(names):
            return role_by_name[names[idx + 1]]
    else:
        for s in stages:
            if s.role != "supervisor":
                return s.role
        return stages[0].role if stages else None
    return None


def _run_banners() -> tuple[str, ...]:
    """The launch banner lines ``read_log_tail`` cuts the tail at."""
    from ._helpers import RUN_BANNERS

    return RUN_BANNERS


def _read_log_tail(
    log_file: Path, n: int, markers: tuple[str, ...] = ()
) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent.

    A12d (incident 31.10): the file is append-only across launches, so the
    tail is cut at the last run banner — one owner for that rule lives in
    ``_helpers.read_log_tail``.
    """
    from ._helpers import read_log_tail

    return read_log_tail(log_file, n, markers=markers)


def _compute_expected_action(
    *,
    pipeline_running: bool,
    current_stage_kind: str | None,
    checkpoint_pending: bool,
    has_active_todos: bool,
    done_count: int,
    last_signal: str | None = None,
    current_stage_name: str | None = None,
) -> str | None:
    """Dogfood-6: structural determinism. Returns ONE instruction string
    telling supervisor what to do NOW, so they don't have to read execution
    model rules from supervisor.md.

    Decision tree based on observable state (not on supervisor remembering rules):
    - pipeline_running=False + last_signal=REVIEW decision → write new TODO, restart
      (a worker's REVIEW-REJECTED signal is not a decision — A32)
    - pipeline_running=False → either start pipeline or create TODO
    - checkpoint_pending=True → wait (user confirms in browser)
    - current_stage_kind='plan' → wait (plan stage, supervisor's plan TODO done)
    - current_stage_kind='execute' → wait (worker running, auto-transition)
    - current_stage_kind='verify' → supervisor must ACK or REVIEW
    """
    # Dogfood-8: REVIEW decision → pipeline exited, supervisor must restart.
    # A32: a worker's REVIEW-REJECTED-<id> shares the prefix but is not a
    # verify decision — the refusal already wrote its findings into the SAME
    # unit, so "write the next TODO" would dispatch the clone A32 forbids.
    if (
        not pipeline_running
        and last_signal
        and is_review_decision(last_signal)
    ):
        return (
            "Pipeline exited after REVIEW rejection. Write the next TODO via "
            "awf_dispatch_todo (with fixes from the REVIEW), then awf_start "
            "to resume. The new TODO must address the issues that caused rejection."
        )

    if not pipeline_running:
        if not has_active_todos:
            return (
                "no active TODO + pipeline not running — write a TODO via "
                "awf_dispatch_todo, then awf_start"
            )
        return "active TODO exists but pipeline not running — call awf_start"

    if checkpoint_pending:
        return (
            "BD-36 checkpoint form is open in user's browser — WAIT for user "
            "confirmation. Do NOT call awf_approve (that's for verify stage) "
            "and do NOT search list_pending_forms (BD-36 is not there). "
            "Poll awf_status; when checkpoint_pending becomes False, pipeline "
            "continues. Tell user the checkpoint_form_url so they can open it."
        )

    if current_stage_kind == "plan":
        return (
            "pipeline at plan stage (BD-36 not yet open or already resolved) — "
            "wait for next status update"
        )

    if current_stage_kind == "execute":
        # R6/AC22–AC25: ONE waiting rule. The old text held both rules in
        # one answer ("do NOT poll in a loop" + "keep the run loop") and
        # the brief clipped the second half — status and card disagreed
        # for the same state. ``running_wait_advice`` is the same owner
        # ``next_action_from_status`` uses.
        from ..brief import running_wait_advice

        return running_wait_advice(current_stage_name or "?")

    if current_stage_kind == "verify":
        return (
            "VERIFY STAGE — pipeline waiting for YOUR decision. Read DONE "
            "reports (outbox/DONE-*.md), check git diff, verify artifact "
            "quality. Then either awf_approve (continue) or write REVIEW-*.md "
            "to replan."
        )

    return None


def _compute_stage_kind(
    project_dir: Path,
    current_stage_name: str | None,
) -> str | None:
    """Read pipeline.yaml and figure out kind of current stage.

    Awf's orchestrator computes kind from position:
      - First stage: plan (supervisor)
      - Last stage: verify (supervisor)
      - Middle stages: execute (worker)

    Returns 'plan' | 'execute' | 'verify' | None.
    """
    if not current_stage_name:
        return None
    stages = _load_pipeline_stages(project_dir)
    for s in stages:
        if s.name == current_stage_name:
            return s.kind
    return None

