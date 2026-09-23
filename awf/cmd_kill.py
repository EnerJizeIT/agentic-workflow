"""Entry point for ``awf kill`` — recovery without MCP (RUN9 #1).

The MCP tool ``awf_kill`` had no CLI twin, so with the MCP server down a
stuck pipeline could not be stopped from the terminal — the doctrine
recipe «MCP / network hangs → python3 -m awf …» pointed at commands that
did not include kill. This command calls the SAME
:func:`awf.api.kill_pipeline` the MCP tool uses (RUN8 #2: the pipeline
AND its stage worker; one API call for both entrances, no duplicated
logic).

Return codes:
- 0 — nothing running / the process already exited / killed;
- 1 — no ``.agentic/`` at the given path, or the kill failed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import api
from .api._errors import AwfApiError
from .api._helpers import require_agentic


def run(args: Any) -> int:
    """Kill the running pipeline (and its worker) and return the rc."""
    project_dir = Path(getattr(args, "project_dir", "."))
    try:
        require_agentic(project_dir)
    except AwfApiError as e:
        print(f"ERROR: {e}")
        return 1

    result = api.kill_pipeline(project_dir)
    print(result["message"])
    if result.get("killed"):
        return 0
    # "No running pipeline found." (pid is None) and "PID X already
    # exited" are harmless no-ops (the state was cleared either way);
    # only an explicit kill failure is an error.
    return 1 if "Failed to kill" in result["message"] else 0
