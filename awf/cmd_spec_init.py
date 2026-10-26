"""``awf spec init <slug>`` — generate a slug-specific spec pipeline.

Writes ONLY ``.agentic/pipelines/spec-<slug>.yaml`` from the packaged
``spec`` template: the template's package token becomes the slug, so the
six author stages write ``docs/spec/<slug>/<slug>.*``. config.yaml and
supervisor.md are not touched; an existing pipeline is refused without
``--force``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api
from .api.setup import write_spec_pipeline


def run(args: Any) -> int:
    """Execute ``awf spec init`` and return the exit code."""
    project_dir = Path(getattr(args, "project", ".")).resolve()
    slug = str(getattr(args, "slug", "") or "").strip()

    try:
        result, unresolved = write_spec_pipeline(
            project_dir, slug, force=bool(getattr(args, "force", False))
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
    print(f"Spec package: docs/spec/{slug}/{slug}.*")
    print(f"Run it with: awf start --pipeline {result.name}")
    return 0
