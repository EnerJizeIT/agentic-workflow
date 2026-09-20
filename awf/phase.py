"""SMO: Phase detection and phase-prompt assembly.

Determines the current supervisor phase from project state, and assembles
a compact phase-specific prompt (replacing the 600+ line supervisor.md).

Phases (setup flow → execute flow):
  init → goal → form → normalize → brief → run → verify → done

Each phase has:
- Entry conditions (deterministic, based on project state)
- A compact prompt (~50-100 lines from templates/phases/phase-<name>.md)
- Exit conditions (signal or state transition)

Backward compat: if phase files don't exist, falls back to supervisor.md.
"""
from __future__ import annotations

from pathlib import Path

from .pipeline import load_stages, resolve_pipeline_file
from .pipeline_state import is_state_stale, read_state

# Phase names in order
SETUP_PHASES = ["init", "goal", "form", "normalize", "brief"]
EXECUTE_PHASES = ["run", "verify"]
ALL_PHASES = SETUP_PHASES + EXECUTE_PHASES + ["done"]


def detect_phase(project_dir: Path) -> str:
    """Determine current supervisor phase from project state.

    Priority:
    1. Explicit `phase` in non-stale state file
    2. Pipeline stage kind (plan/execute/verify)
    3. Setup-flow detection (init→goal→form→normalize→brief)
    """
    project_dir = Path(project_dir).resolve()
    state = read_state(project_dir)

    # Check explicit phase in state
    if state and not is_state_stale(state):
        phase = state.get("phase")
        if phase and phase in ALL_PHASES:
            return phase

        # Infer from pipeline stage
        stage_kind = state.get("stage_kind")
        if stage_kind == "plan":
            # Plan stage = writing the TODO
            return "brief"
        if stage_kind == "execute":
            return "run"
        if stage_kind == "verify":
            return "verify"

    # ── Setup flow detection ──────────────────────────────────────────

    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        return "init"

    # Check goal
    goal = state.get("goal") if state else None
    if not goal:
        return "goal"

    # Check pipeline configuration
    has_pipeline = _pipeline_exists(project_dir)
    if not has_pipeline:
        return "form"

    # Check normalization
    normalized = state.get("normalized") if state else None
    if not normalized:
        return "normalize"

    # Check for active TODO
    from .todos import newest_active
    has_active_todo = bool(newest_active(project_dir))
    if not has_active_todo:
        return "brief"

    # TODO active and pipeline not running → could be between runs
    return "run"


def _pipeline_exists(project_dir: Path) -> bool:
    """Check if pipeline.yaml exists with non-supervisor stages."""
    try:
        pipeline_file = resolve_pipeline_file(project_dir)
        stages = load_stages(pipeline_file)
        return any(s.role != "supervisor" for s in stages)
    except Exception:
        return False


def get_phase_prompt(phase: str, project_dir: Path) -> str:
    """Assemble compact prompt for the given phase.

    Returns _core.md + phase-<name>.md content.
    Falls back to full supervisor.md if phase files don't exist.
    """
    project_dir = Path(project_dir).resolve()

    # Try phase-specific files
    templates_dir = project_dir / "templates" / "roles" / "supervisor"
    core_path = templates_dir / "_core.md"
    phase_path = templates_dir / f"phase-{phase}.md"

    # Also check awf's own templates
    if not core_path.is_file():
        import awf
        awf_templates = Path(awf.__file__).parent / "templates" / "roles" / "supervisor"
        core_path = awf_templates / "_core.md"
        phase_path = awf_templates / f"phase-{phase}.md"

    if core_path.is_file():
        parts = [core_path.read_text(encoding="utf-8")]
        if phase_path.is_file():
            parts.append(phase_path.read_text(encoding="utf-8"))

        # Inject state context
        state = read_state(project_dir)
        goal = state.get("goal") if state else None
        if goal:
            parts.append(f"\n---\n## Goal for this session\n{goal}\n")

        return "\n".join(parts)

    # Fallback: full supervisor.md (backward compat)
    supervisor_md = project_dir / "templates" / "roles" / "supervisor.md"
    if not supervisor_md.is_file():
        import awf
        supervisor_md = Path(awf.__file__).parent / "templates" / "roles" / "supervisor.md"
    if supervisor_md.is_file():
        return supervisor_md.read_text(encoding="utf-8")

    return f"Phase: {phase}. No prompt template found."


def advance_phase(project_dir: Path, **extra_fields) -> str:
    """Advance to the next phase in the setup flow.

    Called by awf tools (awf_set_goal, awf_confirm_normalize, etc.) to
    transition between setup phases. Writes new phase to state.

    Returns the new phase name.
    """
    current = detect_phase(project_dir)

    # Setup flow progression
    if current in SETUP_PHASES:
        idx = SETUP_PHASES.index(current)
        if idx + 1 < len(SETUP_PHASES):
            new_phase = SETUP_PHASES[idx + 1]
        else:
            new_phase = "run"  # brief → run
    elif current == "run":
        new_phase = "verify"
    elif current == "verify":
        new_phase = "done"
    else:
        new_phase = current  # no change

    from .pipeline_state import write_state
    write_state(project_dir, phase=new_phase, **extra_fields)
    return new_phase


__all__ = ["detect_phase", "get_phase_prompt", "advance_phase", "ALL_PHASES"]
