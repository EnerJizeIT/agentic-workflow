"""Configuration via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Config:
    """Resolved plugin configuration."""
    inputs_dir: Path
    templates_dir: Path
    dashboards_dir: Path
    http_port: int
    open_browser_cmd: str
    temp_dir: Path
    default_ttl_seconds: int


def load() -> Config:
    """Load config from environment variables.

    Paths are resolved against the current working directory (which is the
    opencode project root when the plugin runs).
    """
    cwd = Path.cwd()

    def _path(env_var: str, default: str) -> Path:
        value = os.environ.get(env_var, default)
        p = Path(value)
        if not p.is_absolute():
            p = (cwd / p).resolve()
        return p

    return Config(
        inputs_dir=_path("AWF_INPUTS_DIR", ".agentic/inputs"),
        templates_dir=_path("AWF_TEMPLATES_DIR", ".agentic/templates"),
        dashboards_dir=_path("AWF_DASHBOARDS_DIR", ".agentic/dashboards"),
        http_port=int(os.environ.get("AWF_HTTP_PORT", "0")),
        open_browser_cmd=os.environ.get("AWF_OPEN_BROWSER_CMD", "auto"),
        temp_dir=_path("AWF_TEMP_DIR", "/tmp"),
        default_ttl_seconds=int(os.environ.get("AWF_DEFAULT_TTL_SECONDS", "86400")),
    )


def ensure_directories(config: Config) -> None:
    """Create runtime directories if missing. Idempotent."""
    config.inputs_dir.mkdir(parents=True, exist_ok=True)
    config.templates_dir.mkdir(parents=True, exist_ok=True)
    config.dashboards_dir.mkdir(parents=True, exist_ok=True)
