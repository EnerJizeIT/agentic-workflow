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
) -> tuple[str | None, str | None, str | None, str | None]:
    """Read pipeline.yaml + log_tail to figure out current state.

    Returns ``(current_stage_name, next_stage_role, last_signal, log_tail)``.

    ``current_stage_name`` is inferred from orchestrator log (last
    '=== stage=' line). ``next_stage_role`` is the role of the stage
    AFTER current (or first stage if pipeline not started).
    """
    log_file = paths.agentic_dir(project_dir) / "logs" / "awf-start.out"
    log_tail_text = None
    current_stage: str | None = None
    last_signal: str | None = None

    if log_file.is_file():
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
            lines = log_text.splitlines()
            if lines:
                log_tail_text = "\n".join(lines[-30:])

            # Scan log for stage transitions. Format (from orchestrator.py:361):
            #   "  Stage 1/3: agent-system-analyst (agent-system-analyst :: execute)"
            # Also accept older markers for forward-compat.
            import re

            stage_pattern = re.compile(
                r"Stage\s+\d+/\d+:\s+(\S+)"
            )
            for line in lines:
                # Preferred: "Stage N/M: <name>" → capture name
                m = stage_pattern.search(line)
                if m:
                    current_stage = m.group(1)
                    continue
                # Legacy / alternative markers
                for marker in ("=== stage:", "Pipeline stage:", "Entering stage:"):
                    if marker in line:
                        idx = line.find(marker) + len(marker)
                        rest = line[idx:].strip().strip("=").strip()
                        if rest:
                            current_stage = rest.split()[0]
                            break
                # Match signals: "signal detected", "Signal received:", "signal:"
                # Case-insensitive to handle different log styles.
                line_lower = line.lower()
                if "signal detected" in line_lower or "signal received" in line_lower or "signal:" in line_lower:
                    for sig_marker in ("DONE-", "BLOCKED-", "REVIEW-", "TODO-", "ACK-"):
                        if sig_marker in line:
                            idx = line.rfind(sig_marker)
                            rest = line[idx:].split()[0].rstrip(":.,")
                            # Strip common file extensions
                            for ext in (".ready", ".md", ".yaml"):
                                if rest.endswith(ext):
                                    rest = rest[: -len(ext)]
                            last_signal = rest
                            break
        except OSError:
            pass

    # Determine next_stage_role from pipeline.yaml
    next_stage_role: str | None = None
    pipeline_file = (project_dir / ".agentic" / "pipelines" / "default.yaml")
    if pipeline_file.is_file():
        try:
            import yaml

            data = yaml.safe_load(pipeline_file.read_text(encoding="utf-8"))
            stages = (data or {}).get("stages", []) if isinstance(data, dict) else []
            role_by_name = {s.get("name"): s.get("role") for s in stages if isinstance(s, dict)}

            if current_stage and current_stage in role_by_name:
                # Find next stage after current
                names = list(role_by_name.keys())
                if current_stage in names:
                    idx = names.index(current_stage)
                    if idx + 1 < len(names):
                        next_stage_role = role_by_name[names[idx + 1]]
            elif stages:
                # Pipeline not started or unknown stage → first execute stage
                for s in stages:
                    if isinstance(s, dict) and s.get("role") != "supervisor":
                        next_stage_role = s.get("role")
                        break
                if next_stage_role is None and stages:
                    # All supervisor stages? Take the plan one
                    next_stage_role = stages[0].get("role") if isinstance(stages[0], dict) else None
        except (yaml.YAMLError, OSError):
            pass

    return current_stage, next_stage_role, last_signal, log_tail_text


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
    current_stage, next_role, last_signal, log_tail_stage = _extract_stage_info(project_dir)
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
    )


__all__ = ["load_supervisor_context"]
