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

    variant = str(getattr(args, "variant", "") or "").strip()
    if variant:
        return _write_variant(project_dir, args.name, variant, args.force)

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


def _write_variant(project_dir: Path, name: str, variant: str, force: bool) -> int:
    """Write a built-in starter pipeline (the setup form's variant path).

    The composition (template lookup, stage validation, description) lives
    in ``awf.api.setup.write_variant_pipeline`` — the same public entry the
    setup flow's builder uses. This command only formats the answer; it
    reaches into no private helper. The written text is the preview text
    (``pipeline_variant_yaml_text``), by construction of that one builder.

    The side-effect contract is unchanged: only the pipeline file is
    touched; a missing role file is reported, never materialized
    (materialization belongs to the setup/refresh flow).
    """
    from .api.setup import write_variant_pipeline

    try:
        result, unresolved = write_variant_pipeline(
            project_dir, name, variant, force=force
        )
    except (ValueError, api.AwfApiError) as e:
        print(f"ERROR: {e}")
        return 1

    for role in unresolved:
        print(
            f"WARNING: role '{role}' has no role file (checked "
            f".agentic/roles/ and the global awf roles dir) — its "
            f"stage will fail at runtime.",
        )

    verb = "Overwrote" if result.overwritten else "Wrote"
    print(f"{verb} pipeline {result.name!r} ({result.stages} stages) → {result.file}")
    print("config.yaml and supervisor.md were not touched.")
    return 0
