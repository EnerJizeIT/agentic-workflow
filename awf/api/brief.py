"""RUN4 #1 (TODO-0051): public API for the supervisor brief card."""
from __future__ import annotations

from pathlib import Path

from .. import paths
from ..brief import BriefResult, build_brief
from ..run_plan_read import read_run_record
from .lifecycle import get_status
from .run import run_brief


def brief(project_dir: Path) -> BriefResult:
    """Assemble the supervisor onboarding/recovery card (RUN4 #1).

    Live state, not a static document: header (awf version, project,
    phase, date), what's next, state (run: goal, position, budget, last
    decision, sources; active TODOs, blocked/salvage flags, last
    signal), the tool map by situation, rituals, recovery recipes,
    what's new.

    A project without ``.agentic/`` — empty, new, or the directory does
    not exist yet — does NOT raise: the card degrades to header + setup
    hint + tool map (an info tool, no mutations).

    Deterministic for the same state: two calls give identical text
    except the date line (pinned by tests).

    ORCH M1.2: the run record comes from the shared reader
    (``awf/run_plan_read.py``) — the card is the compact view of the same
    record ``awf_load_supervisor_context`` renders in full.
    """
    project_dir = Path(project_dir).expanduser().resolve()
    has_agentic = paths.agentic_dir(project_dir).is_dir()
    status = get_status(project_dir) if has_agentic else None
    run = run_brief(project_dir) if has_agentic else None
    record = read_run_record(project_dir) if has_agentic else None
    return build_brief(project_dir, status=status, run=run, run_record=record)
