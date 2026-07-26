"""Pipeline parsing — Stage dataclass, load_stages, resolve_pipeline_file."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


@dataclass
class Stage:
    name: str
    role: str
    action: str
    description: str = ""
    on_blocked: str = "escalate"
    on_approved: str = "next"
    on_rejected: str = "rollback_to:implement"
    on_passed: str = "next"
    on_failed: str = "rollback_to:implement"
    max_retries: int = 1


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


def load_stages(pipeline_file: str | Path) -> list[Stage]:
    """Parse a pipeline YAML file and return a list of Stage objects."""
    import yaml

    p = Path(pipeline_file)
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw_stages = (data or {}).get("stages") or []
    result: list[Stage] = []

    for s in raw_stages:
        if not isinstance(s, dict):
            continue
        kwargs: dict[str, Any] = {}
        for key in ("name", "role", "action", "description"):
            kwargs[key] = s.get(key, "")
        for pk in _POLICY_KEYS:
            kwargs[pk] = s.get(pk, _DEFAULTS[pk])
        mr = s.get("max_retries", _DEFAULTS["max_retries"])
        try:
            kwargs["max_retries"] = int(mr)
        except (ValueError, TypeError):
            kwargs["max_retries"] = _DEFAULTS["max_retries"]
        result.append(Stage(**kwargs))

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
