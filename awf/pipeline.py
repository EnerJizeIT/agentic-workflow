"""Pipeline parsing — Stage dataclass, load_stages, resolve_pipeline_file.

BD-29: pipeline stages no longer have an `action` field. Awf computes the
kind (plan / execute / verify) from the stage's position:

- Stage index 0           → kind="plan"   (supervisor creates TODO)
- Stage index N-1         → kind="verify" (supervisor verifies + commits)
- All stages between      → kind="execute" (agents do work)

For backward compat, pipeline.yaml files written before BD-29 may still
carry an `action:` field — it's read but ignored. kind always wins.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


@dataclass
class Stage:
    """A single stage in a pipeline.

    `kind` is computed at load time from position (see module docstring).
    It is NOT in pipeline.yaml.
    """
    name: str
    role: str
    description: str = ""
    on_blocked: str = "escalate"
    on_approved: str = "next"
    on_rejected: str = "rollback_to:implement"
    on_passed: str = "next"
    on_failed: str = "rollback_to:implement"
    max_retries: int = 1
    # Computed at load time — not in YAML.
    kind: str = "execute"  # "plan" | "execute" | "verify"


_DEFAULTS: dict[str, Any] = {
    "on_blocked": "escalate",
    "on_approved": "next",
    "on_rejected": "rollback_to:implement",
    "on_passed": "next",
    "on_failed": "rollback_to:implement",
    "max_retries": 1,
}

_POLICY_KEYS = [
    "on_blocked", "on_approved", "on_rejected", "on_passed", "on_failed",
]


def _compute_kind(position: int, total: int) -> str:
    """BD-29: compute kind from position in the pipeline.

    - First stage  → "plan"
    - Last stage   → "verify"
    - Middle       → "execute"
    - Single stage → "plan" (degenerate — no agents, just supervisor creating+committing)

    Special case: if the stage's role is "supervisor" AND it's not the first
    or last, kind is still computed by position — but callers should validate
    that supervisor only appears at endpoints.
    """
    if total <= 1:
        return "plan"
    if position == 0:
        return "plan"
    if position == total - 1:
        return "verify"
    return "execute"


def load_stages(pipeline_file: str | Path) -> list[Stage]:
    """Parse a pipeline YAML file and return a list of Stage objects.

    BD-29: kind is computed from position; `action:` field in YAML is read
    for back-compat but ignored.
    """
    import yaml

    p = Path(pipeline_file)
    try:
        with p.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: pipeline file {p.name} is malformed: {e}", file=sys.stderr)
        return []

    raw_stages = (data or {}).get("stages") or []
    result: list[Stage] = []
    total = len([s for s in raw_stages if isinstance(s, dict)])

    dict_index = 0  # position among dict-entries only (for _compute_kind)
    for s in raw_stages:
        if not isinstance(s, dict):
            continue
        kwargs: dict[str, Any] = {}
        for key in ("name", "role", "description"):
            kwargs[key] = s.get(key, "")
        for pk in _POLICY_KEYS:
            kwargs[pk] = s.get(pk, _DEFAULTS[pk])
        mr = s.get("max_retries", _DEFAULTS["max_retries"])
        try:
            kwargs["max_retries"] = int(mr)
        except (ValueError, TypeError):
            kwargs["max_retries"] = _DEFAULTS["max_retries"]
        kwargs["kind"] = _compute_kind(dict_index, total)
        result.append(Stage(**kwargs))
        dict_index += 1

    return result


def resolve_pipeline_file(
    project_dir: str | Path,
    pipeline_name: str | None = None,
    config: dict | None = None,
) -> Path:
    """Determine which pipeline file to use.

    Order:
    1. Explicit *pipeline_name* → .agentic/pipelines/<name>.yaml
    2. default_pipeline from config.yaml → .agentic/pipelines/<that>.yaml
    3. Fallback .agentic/pipelines/default.yaml
    """
    project_dir = Path(project_dir).resolve()
    agentic = paths.agentic_dir(project_dir)
    pipelines_dir = agentic / "pipelines"

    if config is None:
        config = cfg_mod.load(project_dir)

    name = pipeline_name or cfg_mod.get(config, "default_pipeline", "default") or "default"

    candidate = pipelines_dir / f"{name}.yaml"
    if candidate.exists():
        return candidate

    if name != "default":
        fallback = pipelines_dir / "default.yaml"
        if fallback.exists():
            return fallback

    raise FileNotFoundError(
        f"No pipeline file found. Expected {candidate}"
        + (f" or {fallback}" if name != "default" else "")
    )
