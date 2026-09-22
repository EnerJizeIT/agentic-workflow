"""``awf pipeline-write`` — create a named pipeline file (RUN3 #1).

Stages are generated mechanically from the given roles, exactly the way
the project-setup form does it (``build_pipeline_stages``): supervisor
``plan`` → one stage per role → supervisor ``verify``. Only the pipeline
file is written — config.yaml and supervisor.md are not touched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api
from .api._helpers import slugify_role


def run(args: Any) -> int:
    """Execute ``awf pipeline-write`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    # De-duplicate roles by slug (order kept) — a duplicate would be
    # skipped silently inside build_pipeline_stages.
    seen: set[str] = set()
    team: list[dict[str, Any]] = []
    for raw in getattr(args, "role", []) or []:
        slug = slugify_role(str(raw))
        if slug in seen:
            continue
        seen.add(slug)
        team.append({"role": raw})
    if not team:
        print("ERROR: at least one --role is required")
        return 1

    from .api.setup import build_pipeline_stages

    try:
        stages = build_pipeline_stages(team)
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1

    try:
        result = api.write_pipeline(project_dir, args.name, stages, force=args.force)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    # AUD06-15 parity: the same warning the setup form emits — a role
    # without a role file would fail its stage at runtime.
    try:
        from .api.setup import _unresolved_team_roles

        for role in _unresolved_team_roles(team, project_dir):
            print(
                f"WARNING: role '{role}' has no role file (checked "
                f".agentic/roles/ and the global awf roles dir) — its stage "
                f"will fail at runtime.",
            )
    except Exception:
        pass  # the warning is best-effort; the write already succeeded

    verb = "Overwrote" if result.overwritten else "Wrote"
    print(f"{verb} pipeline {result.name!r} ({result.stages} stages) → {result.file}")
    print("config.yaml and supervisor.md were not touched.")
    return 0
