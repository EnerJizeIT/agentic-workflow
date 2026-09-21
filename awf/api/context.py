"""Supervisor context loader: one-shot aggregate for session bootstrap.

``load_supervisor_context`` returns everything a supervisor needs to act
in a single call — no manual file reads, no multiple tool round-trips.
Used at start of session or after long pause to rebuild mental model.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .. import config as cfg_mod
from .. import paths
from ..pipeline import Stage, load_stages, resolve_pipeline_file
from ._background import check_pipeline_running
from ._helpers import read_file_text, require_agentic
from ._results import SupervisorContextResult
from .lifecycle import get_status


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
        paths.agentic_dir(project_dir) / "logs" / "awf-start.out", 30
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


def _read_log_tail(log_file: Path, n: int) -> str | None:
    """Read last N lines of a log file. Returns None if file is absent."""
    if not log_file.is_file():
        return None
    try:
        text = log_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines:
        return ""
    tail = lines[-n:] if len(lines) > n else lines
    return "\n".join(tail)


def _compute_expected_action(
    *,
    pipeline_running: bool,
    current_stage_kind: str | None,
    checkpoint_pending: bool,
    has_active_todos: bool,
    done_count: int,
    last_signal: str | None = None,
) -> str | None:
    """Dogfood-6: structural determinism. Returns ONE instruction string
    telling supervisor what to do NOW, so they don't have to read execution
    model rules from supervisor.md.

    Decision tree based on observable state (not on supervisor remembering rules):
    - pipeline_running=False + last_signal=REVIEW → write new TODO, restart
    - pipeline_running=False → either start pipeline or create TODO
    - checkpoint_pending=True → wait (user confirms in browser)
    - current_stage_kind='plan' → wait (plan stage, supervisor's plan TODO done)
    - current_stage_kind='execute' → wait (worker running, auto-transition)
    - current_stage_kind='verify' → supervisor must ACK or REVIEW
    """
    # Dogfood-8: REVIEW signal → pipeline exited, supervisor must restart
    if (
        not pipeline_running
        and last_signal
        and last_signal.startswith("REVIEW-")
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
        return (
            "worker stage running — auto-transition on worker DONE. Do NOT "
            "intervene and do NOT poll in a loop: check awf_status once when "
            "you need to act. In an active run, keep the run loop instead: "
            "awf_wait_for_event(actionable_only=True)."
        )

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


def _read_role_prohibitions(project_dir: Path, role: str | None) -> str | None:
    """Extract prohibitions section from role .md (first 1000 chars of any
    section titled 'Prohibitions' / 'DO NOT' / 'Out of scope').

    Returns None if role is None, file missing, or section not found.
    """
    if not role:
        return None

    role_md = project_dir / ".agentic" / "roles" / f"{role}.md"
    if not role_md.is_file():
        return None

    try:
        content = role_md.read_text(encoding="utf-8")
    except OSError:
        return None

    # Find any section that looks like prohibitions
    prohibitions_markers = (
        "## Prohibitions",
        "## What NOT to do",
        "## Out of scope",
        "## DO NOT",
        "## Не делай",
    )
    for marker in prohibitions_markers:
        idx = content.find(marker)
        if idx == -1:
            continue
        # Take until next ## section or end
        rest = content[idx:]
        next_section = rest.find("\n## ", len(marker))
        if next_section != -1:
            rest = rest[:next_section]
        # Trim to 1000 chars to keep payload small
        if len(rest) > 1000:
            rest = rest[:1000] + "\n...[truncated]"
        return rest.strip()

    return None


def _is_plan_stub(plan_md: str) -> bool:
    """Dogfood-9: detect if plan.md is the init template stub.

    init_project writes a stub with markers:
      - '# {project_name} — Plan'
      - 'supervisor заполнит после изучения vision'
      - OR 'supervisor заполнит'
      - OR 'Спроси пользователя о контексте'

    apply_increment_plan replaces stub with real plan + frontmatter.
    No frontmatter + stub markers → True.
    """
    if not plan_md:
        return True
    # Has frontmatter → already materialized (not stub)
    if plan_md.lstrip().startswith("---"):
        return False
    stub_markers = [
        "supervisor заполнит",
        "Спроси пользователя о контексте",
        "(supervisor заполнит",
    ]
    return any(marker in plan_md for marker in stub_markers)


def _get_final_stage_commit_policy(project_dir: Path) -> str | None:
    """Dogfood-9: read on_approved from last stage of pipeline.yaml.

    Returns 'commit_and_next' / 'commit_and_report' / 'next' / None.
    Supervisor needs to know if auto-commit will fire.
    """
    stages = _load_pipeline_stages(project_dir)
    if not stages:
        return None
    last = stages[-1]
    # AUD16-03: on_passed is gone (no TEST-PASSED signal) — on_approved alone.
    return last.on_approved


def _pipeline_exists(project_dir: Path) -> bool:
    """Check if pipeline.yaml exists with non-supervisor stages."""
    stages = _load_pipeline_stages(project_dir)
    return any(s.role != "supervisor" for s in stages)


def load_supervisor_context(project_dir: Path) -> SupervisorContextResult:
    """Aggregate everything a supervisor needs in one call.

    Replaces 5-6 separate tool calls at session start:
    - vision excerpt
    - plan.md content
    - supervisor.md content (role instruction)
    - active_todos + progress
    - pipeline state (running, current_stage, log_tail)
    - next_stage_role + its prohibitions excerpt
    - git diff stat

    Designed for these moments:
    - Start of new supervisor session (after awf_init)
    - After long pause (rebuild mental model)
    - Before writing next TODO (need full context)

    Args:
        project_dir: awf project root.

    Returns:
        SupervisorContextResult with all fields populated.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    config_data = cfg_mod.load(project_dir)
    project_name = (
        cfg_mod.get(config_data, "project.name", "Project") or "Project"
    )

    # Vision
    vision_path = paths.find_vision_file(project_dir)
    vision_excerpt = read_file_text(vision_path, max_chars=4000) if vision_path else ""

    # Plan + supervisor.md
    plan_path = project_dir / ".agentic" / "phases" / "plan.md"
    plan_md = read_file_text(plan_path) if plan_path.is_file() else ""

    # SMO: detect phase + return compact prompt (not full 639-line supervisor.md).
    # Dogfood finding: supervisor reads full supervisor.md and ignores phase system.
    try:
        from ..phase import detect_phase, get_phase_prompt
        phase = detect_phase(project_dir)
        phase_prompt = get_phase_prompt(phase, project_dir)
    except Exception:
        phase = "unknown"
        phase_prompt = ""

    supervisor_md = phase_prompt  # compact, not full

    # Status (active todos, done/blocked counts)
    status = get_status(project_dir)

    # Pipeline state
    pipeline_running, pipeline_pid, _log_tail_bg = check_pipeline_running(project_dir)

    # Stage info from logs + pipeline.yaml
    current_stage, next_role, last_signal, log_tail_stage, _cp, _cpp, _cfurl = _extract_stage_info(project_dir)
    # Prefer log_tail from check_pipeline_running (more recent)
    log_tail = _log_tail_bg or log_tail_stage

    # Role prohibitions for next stage
    next_role_prohibitions = _read_role_prohibitions(project_dir, next_role)

    # Git diff stat
    diff_result = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=str(project_dir),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    git_diff_stat = diff_result.stdout if diff_result.returncode == 0 else ""

    warnings: list[str] = []
    if not vision_path:
        warnings.append("Vision/README not found in project root")
    if not plan_md:
        warnings.append("plan.md is empty or missing")
    if not supervisor_md:
        warnings.append("supervisor.md is empty or missing")
    if next_role and not next_role_prohibitions:
        warnings.append(
            f"next_stage_role='{next_role}' has no prohibitions section in .md"
        )

    # Dogfood-9: structural triggers for increment planning + commit visibility
    pipeline_configured = _pipeline_exists(project_dir)
    increment_planning_needed = (
        pipeline_configured
        and not pipeline_running
        and not status.active_todos  # no active TODOs in flight
        and _is_plan_stub(plan_md)
    )
    final_commit_policy = _get_final_stage_commit_policy(project_dir)

    return SupervisorContextResult(
        project_name=project_name,
        project_dir=str(project_dir),
        vision_path=str(vision_path) if vision_path else None,
        vision_excerpt=vision_excerpt,
        plan_md=plan_md,
        supervisor_md=supervisor_md,
        phase=phase,
        phase_prompt=phase_prompt,
        active_todos=status.active_todos,
        done_count=status.done_count,
        blocked_count=status.blocked_count,
        pipeline_running=pipeline_running,
        pipeline_pid=pipeline_pid,
        log_tail=log_tail,
        next_stage_role=next_role,
        next_role_prohibitions=next_role_prohibitions,
        current_stage_name=current_stage,
        last_signal=last_signal,
        git_diff_stat=git_diff_stat,
        warnings=warnings,
        increment_planning_needed=increment_planning_needed,
        final_stage_commit_policy=final_commit_policy,
    )


__all__ = ["load_supervisor_context"]
