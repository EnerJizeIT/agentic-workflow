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
from ._background import check_pipeline_running
from ._helpers import read_file_text, require_agentic
from ._results import SupervisorContextResult
from .lifecycle import get_status


def _extract_stage_info(
    project_dir: Path,
) -> tuple[str | None, str | None, str | None, str | None, bool, int | None, str | None]:
    """Read pipeline state from structured file first, fall back to regex.

    T4.1: orchestrator + plan_checkpoint persist structured state to
    ``.agentic/state/current.yaml`` after each transition. This function
    reads that file — single source of truth, no fragile regex.

    Falls back to regex parsing of ``awf-start.out`` if state file
    missing (older pipeline run, or pipeline_state writes failed).

    Returns ``(current_stage_name, next_stage_role, last_signal, log_tail,
    checkpoint_pending, checkpoint_port, checkpoint_form_url)``.
    """
    # ── T4.1: structured state file (preferred) ────────────────────────
    from ..pipeline_state import is_state_stale, read_state

    state = read_state(project_dir)
    log_tail_text = _read_log_tail(
        paths.agentic_dir(project_dir) / "logs" / "awf-start.out", 30
    )
    if state and not is_state_stale(state):
        # All fields available from structured state — no regex needed.
        current_stage = state.get("stage_name")
        checkpoint_pending = bool(state.get("checkpoint_pending", False))
        checkpoint_port = state.get("checkpoint_port")
        checkpoint_form_url = state.get("checkpoint_form_url")
        last_signal = state.get("last_signal")

        # next_stage_role still needs pipeline.yaml lookup
        next_stage_role = _lookup_next_stage_role(project_dir, current_stage)

        # last_signal: prefer state, fall back to regex if missing
        if not last_signal:
            _, _, last_signal, _, _, _, _ = _extract_stage_info_regex(project_dir)

        return (
            current_stage,
            next_stage_role,
            last_signal,
            log_tail_text,
            checkpoint_pending,
            checkpoint_port if checkpoint_port is not None else None,
            checkpoint_form_url,
        )

    # ── Fallback: regex log parsing (pre-T4.1 behaviour) ──────────────
    return _extract_stage_info_regex(project_dir, log_tail_text)


def _lookup_next_stage_role(project_dir: Path, current_stage: str | None) -> str | None:
    """Look up next stage role from pipeline.yaml based on current stage."""
    pipeline_file = project_dir / ".agentic" / "pipelines" / "default.yaml"
    if not pipeline_file.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
        stages = (data or {}).get("stages", []) if isinstance(data, dict) else []
        role_by_name = {s.get("name"): s.get("role") for s in stages if isinstance(s, dict)}

        if current_stage and current_stage in role_by_name:
            names = list(role_by_name.keys())
            if current_stage in names:
                idx = names.index(current_stage)
                if idx + 1 < len(names):
                    return role_by_name[names[idx + 1]]
        elif stages:
            for s in stages:
                if isinstance(s, dict) and s.get("role") != "supervisor":
                    return s.get("role")
            if stages:
                return stages[0].get("role") if isinstance(stages[0], dict) else None
    except (yaml.YAMLError, OSError):
        pass
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


def _extract_stage_info_regex(
    project_dir: Path,
    log_tail_text: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, bool, int | None, str | None]:
    """Fallback: parse awf-start.out via regex (pre-T4.1 behaviour).

    Kept for backward compatibility with pipelines started before T4.1
    state writes were added. Prefer read_state() in new code.
    """
    log_file = paths.agentic_dir(project_dir) / "logs" / "awf-start.out"
    if log_tail_text is None:
        log_tail_text = _read_log_tail(log_file, 30)
    current_stage: str | None = None
    last_signal: str | None = None
    checkpoint_pending = False
    checkpoint_port: int | None = None
    checkpoint_form_url: str | None = None

    if log_file.is_file():
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            lines = log_text.splitlines()

            import re

            stage_pattern = re.compile(r"Stage\s+\d+/\d+:\s+(\S+)")
            checkpoint_open_pattern = re.compile(
                r"BD-36: checkpoint opened.*on port (\d+)", re.IGNORECASE
            )
            checkpoint_decision_pattern = re.compile(
                r"BD-36: checkpoint (decision|rejected|timeout)", re.IGNORECASE
            )
            checkpoint_form_url_pattern = re.compile(r"BD-36: form_url=(\S+)")
            checkpoint_opened = False
            checkpoint_decided = False
            for line in lines:
                m = stage_pattern.search(line)
                if m:
                    current_stage = m.group(1)
                    checkpoint_opened = False
                    checkpoint_decided = False
                    continue
                if checkpoint_open_pattern.search(line):
                    checkpoint_opened = True
                    pm = checkpoint_open_pattern.search(line)
                    if pm:
                        try:
                            checkpoint_port = int(pm.group(1))
                        except ValueError:
                            pass
                elif checkpoint_decision_pattern.search(line):
                    checkpoint_decided = True
                fm = checkpoint_form_url_pattern.search(line)
                if fm:
                    checkpoint_form_url = fm.group(1)
                for marker in ("=== stage:", "Pipeline stage:", "Entering stage:"):
                    if marker in line:
                        idx = line.find(marker) + len(marker)
                        rest = line[idx:].strip().strip("=").strip()
                        if rest:
                            current_stage = rest.split()[0]
                            break
                line_lower = line.lower()
                if "signal detected" in line_lower or "signal received" in line_lower or "signal:" in line_lower:
                    for sig_marker in ("DONE-", "BLOCKED-", "REVIEW-", "TODO-", "ACK-"):
                        if sig_marker in line:
                            idx = line.rfind(sig_marker)
                            rest = line[idx:].split()[0].rstrip(":.,")
                            for ext in (".ready", ".md", ".yaml"):
                                if rest.endswith(ext):
                                    rest = rest[: -len(ext)]
                            last_signal = rest
                            break
            checkpoint_pending = checkpoint_opened and not checkpoint_decided
        except OSError:
            pass

    next_stage_role = _lookup_next_stage_role(project_dir, current_stage)
    return current_stage, next_stage_role, last_signal, log_tail_text, checkpoint_pending, checkpoint_port, checkpoint_form_url


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
            "intervene. Poll awf_status until current_stage_kind becomes 'verify'."
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
    pipeline_file = project_dir / ".agentic" / "pipelines" / "default.yaml"
    if not pipeline_file.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
        stages = (data or {}).get("stages", []) if isinstance(data, dict) else []
        names = [s.get("name") for s in stages if isinstance(s, dict)]
        if current_stage_name not in names:
            return None
        idx = names.index(current_stage_name)
        if idx == 0:
            return "plan"
        if idx == len(names) - 1:
            return "verify"
        return "execute"
    except (yaml.YAMLError, OSError):
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
    pipeline_file = project_dir / ".agentic" / "pipelines" / "default.yaml"
    if not pipeline_file.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
        stages = (data or {}).get("stages", []) if isinstance(data, dict) else []
        if not stages:
            return None
        last = stages[-1]
        if isinstance(last, dict):
            return last.get("on_approved") or last.get("on_passed")
    except (yaml.YAMLError, OSError):
        pass
    return None


def _pipeline_exists(project_dir: Path) -> bool:
    """Check if pipeline.yaml exists with non-supervisor stages."""
    pipeline_file = project_dir / ".agentic" / "pipelines" / "default.yaml"
    if not pipeline_file.is_file():
        return False
    try:
        import yaml

        data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
        stages = (data or {}).get("stages", []) if isinstance(data, dict) else []
        return any(
            isinstance(s, dict) and s.get("role") != "supervisor"
            for s in stages
        )
    except (yaml.YAMLError, OSError):
        return False


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

    supervisor_md_path = project_dir / ".agentic" / "roles" / "supervisor.md"
    supervisor_md = read_file_text(supervisor_md_path) if supervisor_md_path.is_file() else ""

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
