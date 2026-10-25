"""Entry point for ``awf config-get``.

U2/U3 + REVIEW V1: print one ``config.yaml`` value for scripts.
The exit code carries the distinction a shell needs:

- 0 — value printed (a string raw, anything else as YAML);
- 1 — key is absent;
- 3 — key is set to YAML ``null``;
- 4 — key is a whitespace-only string;
- 5 — ``config.yaml`` is malformed.

3/4 exist because ``test_cmd:`` (null) and ``test_cmd: " "`` are "not
configured", not a command named "null": the canonical-suite helper maps
them to rc 2 instead of running garbage. 5 is separate from 1 (key absent):
a broken file must not silently fall back to the pytest default.

REVIEW architect: this used to live inline in the CLI dispatcher,
breaking the one-module-per-command layout of every other ``awf`` command.
"""
from __future__ import annotations

import sys
from typing import Any

import yaml

from . import config as cfg_mod
from . import paths


def _load_config(project_dir: Any) -> tuple[dict, bool]:
    """Return ``(config, malformed)`` without hiding a broken file.

    ``cfg_mod.load`` degrades a malformed file to ``{}`` with a stderr
    note — right for every API caller, useless for a command whose whole
    job is the difference between "no key" and "cannot read the file".
    """
    cfg_path = paths.config_file(project_dir)
    if not cfg_path.exists():
        return {}, False
    try:
        with cfg_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        print(f"ERROR: config.yaml is malformed: {e}", file=sys.stderr)
        return {}, True
    return (data if isinstance(data, dict) else {}), False


def run(args: Any) -> int:
    """Print one dotted config key; the exit code is the contract above."""
    config, malformed = _load_config(getattr(args, "project_dir", "."))
    if malformed:
        return 5
    missing = object()
    value = cfg_mod.get(config, args.key, missing)
    if value is missing:
        return 1
    if value is None:
        return 3
    if isinstance(value, str) and not value.strip():
        return 4
    if isinstance(value, str):
        print(value)
        return 0
    print(
        yaml.safe_dump(
            value,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        ).rstrip("\n")
    )
    return 0
