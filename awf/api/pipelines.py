"""Named pipelines: create and list (RUN3 #1).

The project-setup form writes the ACTIVE pipeline as a side effect of team
materialization (plus config.yaml / supervisor.md patches). These functions
treat pipelines as first-class objects:

- :func:`write_pipeline` — writes ONLY ``.agentic/pipelines/<name>.yaml``;
  config.yaml and supervisor.md are not touched.
- :func:`list_pipelines` — the available names + the active one.

Running a named pipeline is the existing mechanism:
``awf start --pipeline <name>`` / ``awf_start(pipeline=<name>)`` resolves
``.agentic/pipelines/<name>.yaml`` (:func:`awf.pipeline.resolve_pipeline_file`),
and an unknown name is a clear error with the list of what exists.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .. import config as cfg_mod
from .. import paths
from .._atomic import atomic_write_text
from ..pipeline import (
    active_pipeline_name,
    list_pipeline_names,
    validate_pipeline_stages,
)
from ._errors import AwfApiError
from ._helpers import require_agentic, slugify_role
from ._results import ListPipelinesResult, WritePipelineResult

# A-06: stage schema (allowed keys, types, policies, budgets) is validated
# by the SHARED validator in awf/pipeline.py — the same rule set the
# loader applies to hand-written YAML (AUD13-04: a typo'd key written here
# would be silently ignored by the loader and misbehave at runtime).

# Same rule as resolve_pipeline_file (AUD14-05): letters, digits, '_',
# '.', '-' — no path separators, no spaces, no "." / "..".
_NAME_RE = re.compile(r"[\w.-]+\Z")


def validate_pipeline_name(name: Any) -> str:
    """Validate a pipeline name (traversal-proof). Returns the name.

    Raises:
        AwfApiError: empty, path-like (``/``, ``..``), or containing
            characters outside ``[\\w.-]`` (e.g. spaces).
    """
    name = str(name or "").strip()
    if not name or name in {".", ".."} or not _NAME_RE.fullmatch(name):
        raise AwfApiError(
            f"Invalid pipeline name {name!r}: use letters, digits, '_', '.' "
            "or '-' (no path separators, no spaces), e.g. 'audit-llm'."
        )
    return name


def _validate_stages(stages: Any, source: str) -> list[dict[str, Any]]:
    """Validate/normalize stage objects against the pipeline YAML schema.

    Form validation (list of mappings, unknown keys, role, policies,
    budgets) is the shared A-06 validator (awf/pipeline.py). Here it
    normalizes: ``role`` is slugified, ``name`` defaults to the role slug
    (same slug the runtime uses for role files and models.<role>).
    """
    validated = validate_pipeline_stages(stages, source)
    result: list[dict[str, Any]] = []
    for stage in validated:
        entry = dict(stage)
        entry["role"] = slugify_role(entry["role"])
        entry.setdefault("name", entry["role"])
        result.append(entry)
    return result


def write_pipeline(
    project_dir: Path,
    name: str,
    stages: list[dict[str, Any]],
    *,
    force: bool = False,
) -> WritePipelineResult:
    """Write ``.agentic/pipelines/<name>.yaml`` — and nothing else.

    Side-effect contract (RUN3 #1): the ONLY file touched is the pipeline
    file. config.yaml and supervisor.md are left as-is (the setup form
    patches them; this does not). An existing pipeline is refused without
    ``force=True``.

    Args:
        project_dir: awf project root (must contain .agentic/).
        name: pipeline name (validated — traversal-proof).
        stages: stage objects, pipeline YAML schema (``role`` required;
            ``name`` defaults to the role slug). Unknown keys are refused.
        force: overwrite an existing pipeline file (default: False).

    Raises:
        AwfApiError: no .agentic/, invalid name, invalid stages, or an
            existing file without force.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    name = validate_pipeline_name(name)
    stages = _validate_stages(stages, f"pipeline {name!r}")

    pipelines_dir = paths.pipelines_dir(project_dir)
    pipelines_dir.mkdir(parents=True, exist_ok=True)
    target = pipelines_dir / f"{name}.yaml"

    overwritten = False
    if target.exists():
        if not force:
            raise AwfApiError(
                f"Pipeline {name!r} already exists at {target}. "
                "Pass force=True to overwrite."
            )
        overwritten = True

    content = yaml.safe_dump(
        {"name": name, "stages": stages},
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )
    atomic_write_text(target, content)
    return WritePipelineResult(
        name=name, file=str(target), stages=len(stages), overwritten=overwritten
    )


def list_pipelines(project_dir: Path) -> ListPipelinesResult:
    """List the pipeline files + mark the active one.

    Active = ``default_pipeline`` from config.yaml (validated the way
    :func:`awf.pipeline.resolve_pipeline_file` does), "default" otherwise.
    """
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)
    names = list_pipeline_names(project_dir)
    config = cfg_mod.load(project_dir)
    active = active_pipeline_name(project_dir, config)
    return ListPipelinesResult(
        pipelines=names,
        active=active,
        active_exists=active in names,
    )
