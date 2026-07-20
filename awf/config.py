"""Read .agentic/config.yaml via PyYAML."""
from pathlib import Path

import yaml


def load(project_dir: str | Path = ".") -> dict:
    """Load config.yaml as a dict. Returns empty dict when file is missing."""
    cfg = Path(project_dir).resolve() / ".agentic" / "config.yaml"
    if not cfg.exists():
        return {}
    with cfg.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def get(config: dict, dotted_key: str, default=None):
    """Navigate a dotted path inside *config*, returning *default* on miss."""
    cur = config
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur
