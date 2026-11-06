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
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        # AUD06-02 class: non-UTF-8 bytes / read errors used to escape as
        # UnicodeDecodeError and crash every caller (load is the universal
        # config reader). Degrade to {} like a malformed file.
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


#: A67: the rejection threshold for a reviewer's structural findings. The
#: closed list is ``major`` (default — a major or a blocker rejects) and
#: ``blocker`` ("collect every finding" mode: only a blocker stops the unit).
DEFAULT_FINDINGS_THRESHOLD = "major"
FINDINGS_THRESHOLDS: tuple[str, ...] = (DEFAULT_FINDINGS_THRESHOLD, "blocker")

#: The config path, named once: the registry ratchet scans ``*_KEY`` constants.
FINDINGS_THRESHOLD_KEY = "review.findings_threshold"


def findings_threshold(config: dict) -> str:
    """Read ``review.findings_threshold``; unknown values fall back to major.

    A typo must not silently switch the mode: the fallback is the default
    (the stricter of the two), never ``blocker``.
    """
    value = get(config, FINDINGS_THRESHOLD_KEY, DEFAULT_FINDINGS_THRESHOLD)
    if isinstance(value, str) and value.strip().lower() in FINDINGS_THRESHOLDS:
        return value.strip().lower()
    return DEFAULT_FINDINGS_THRESHOLD
