"""RUN4 #1 (TODO-0051): ``awf brief`` — карточка погружения и восстановления."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import api


def run(args: Any) -> int:
    """Execute ``awf brief`` and return exit code."""
    project_dir = Path(getattr(args, "project_dir", ".")).resolve()

    try:
        result = api.brief(project_dir)
    except api.AwfApiError as e:
        print(str(e))
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.text, end="")
    return 0
