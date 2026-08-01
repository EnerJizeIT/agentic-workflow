"""Port of lib/add-role.sh — ``awf add-role`` command.

Thin CLI wrapper around :func:`awf.api.add_role`.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf add-role`` and return exit code."""
    role_name = args.name
    description = getattr(args, "description", "") or ""
    model = getattr(args, "model", "") or ""
    project_dir = Path(getattr(args, "project_dir", "."))

    if not model:
        model = input(
            f"Model id for role '{role_name}' (e.g. claude-sonnet-4-20250514, gpt-4.1): "
        ).strip()

    try:
        result = api.add_role(
            project_dir=project_dir,
            role_name=role_name,
            description=description,
            model=model,
        )
    except api.AwfApiError as e:
        print(str(e))
        return 1

    print(f"Created: {result.role_file}")
    print()
    print("Next steps:")
    print(f"  1. Edit the role instructions in {result.role_file}")
    print(f"  2. Add model config to .agentic/config.yaml under models:{role_name}")
    print(f"  3. Add a stage to your pipeline YAML that uses role: {role_name}")
    return 0
