"""CARRY IMP2 (R13/K14): one builder for the canonical full-suite command.

The engine and ``scripts/suite-canonical.sh`` must ask the same question and
get the same string, otherwise the artifact the script writes never matches
the command the engine looks for. This module is the single literal home of
the legacy pytest fallback.

Resolution (K14):

- ``verification.test_cmd`` is a non-empty string → ``source="config"``,
  the command exactly as written;
- the key is absent → ``source="legacy"`` with the pytest fallback, the
  interpreter taken from ``AWF_PYTHON`` (or ``python3``);
- ``null``, an empty/whitespace string, broken YAML or a non-mapping
  config → ``None`` ("suite is not configured").

``AWF_SUITE_CMD`` is NOT read here: only the shell script honours that
override, and the engine never treats such an artifact as canonical
(AC34).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config as cfg_mod
from . import paths

CONFIG_SOURCE = "config"
LEGACY_SOURCE = "legacy"

LEGACY_TEMPLATE = (
    "{pybin} -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider"
)


@dataclass(frozen=True)
class SuiteCommand:
    """A resolved canonical command and where it came from."""

    command: str
    source: str


def legacy_command() -> str:
    """The pytest fallback for projects with no ``verification.test_cmd``."""
    pybin = os.environ.get("AWF_PYTHON") or "python3"
    return LEGACY_TEMPLATE.format(pybin=pybin)


def _load_config(project_dir: str | Path) -> tuple[dict, str]:
    """Return ``(config, state)`` with ``state`` in ``ok/missing/broken``.

    ``config.load`` hides a malformed file behind ``{}`` (right for every
    tolerant caller), but here "no key" and "cannot read the file" must stay
    apart: the first is a legacy fallback, the second is "not configured".
    """
    cfg_path = paths.config_file(project_dir)
    if not cfg_path.exists():
        return {}, "missing"
    try:
        with cfg_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return {}, "broken"
    if not isinstance(data, dict):
        return {}, "broken"
    return data, "ok"


def resolve(project_dir: str | Path = ".") -> SuiteCommand | None:
    """Resolve the canonical full-suite command, or ``None`` when unset."""
    config, state = _load_config(project_dir)
    if state == "broken":
        return None
    missing = object()
    value = cfg_mod.get(config, "verification.test_cmd", missing)
    if state == "missing" or value is missing:
        return SuiteCommand(legacy_command(), LEGACY_SOURCE)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return SuiteCommand(value, CONFIG_SOURCE)


__all__ = [
    "CONFIG_SOURCE",
    "LEGACY_SOURCE",
    "LEGACY_TEMPLATE",
    "SuiteCommand",
    "legacy_command",
    "resolve",
]
