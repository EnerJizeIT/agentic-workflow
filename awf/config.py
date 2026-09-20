"""Read .agentic/config.yaml via PyYAML."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

from . import paths


def load(project_dir: str | Path = ".") -> dict:
    """Load config.yaml as a dict. Returns empty dict when file is missing."""
    cfg = paths.config_file(project_dir)
    if not cfg.exists():
        return {}
    try:
        with cfg.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"ERROR: config.yaml is malformed: {e}", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def get(config: dict, dotted_key: str, default=None):
    """Navigate a dotted path inside *config*, returning *default* on miss.

    P3: distinguishes "key = null" from "key missing" — returns None if the key
    exists with value None, returns default only if the key doesn't exist.
    """
    cur = config
    for part in dotted_key.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur  # P3: return actual value (including None), not default
