"""REPORTS26 F6: ``awf commit-workflow`` — workflow-определения в git."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Выполнить ``awf commit-workflow`` и вернуть код выхода."""
    result = api.commit_workflow(Path(args.project_dir))
    print(f"Committed {len(result.files)} workflow file(s) at {result.sha}:")
    for f in result.files:
        print(f"  {f}")
    return 0
