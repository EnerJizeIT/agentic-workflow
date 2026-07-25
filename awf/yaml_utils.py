"""YAML helpers — dotted-path get and stage extraction."""
from __future__ import annotations

from pathlib import Path

import yaml


def yaml_get(file_path: str | Path, dotted_key: str, default=None):
    """Read a scalar from *file_path* at *dotted_key*. Returns *default* on miss."""
    p = Path(file_path)
    if not p.exists():
        return default
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        return default
    cur = data
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur
