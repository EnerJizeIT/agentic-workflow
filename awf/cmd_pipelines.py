"""``awf pipelines`` — list the named pipelines (RUN3 #1).

Shows every ``.agentic/pipelines/*.yaml`` and marks the active one (the
``default_pipeline`` from config.yaml, "default" when undeclared).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf pipelines`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    try:
        result = api.list_pipelines(project_dir)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if not result.pipelines:
        print("No pipelines in .agentic/pipelines/.")
        return 0

    for name in result.pipelines:
        if name == result.active:
            print(f"* {name}  (active)")
        else:
            print(f"  {name}")
    if not result.active_exists:
        print(
            f"Note: the active pipeline {result.active!r} "
            "(config.yaml) has no file yet."
        )
    return 0
