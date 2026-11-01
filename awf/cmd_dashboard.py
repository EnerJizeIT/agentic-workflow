"""``awf dashboard`` — where the live dashboard is, and how to open it.

A14: the pipeline opens the tab itself once per run; this command answers
"where is it" without a browser, and ``--open`` reopens a tab the owner
closed. Both read the same marker file, so the two never open two tabs for
one URL.
"""
from __future__ import annotations

import sys
from pathlib import Path


def run(args) -> int:
    from .api import dashboard_open
    from .api.dashboard_server import start_dashboard_with_reuse

    project_dir = Path(getattr(args, "project_dir", ".")).resolve()
    if not (project_dir / ".agentic").is_dir():
        print(f"ERROR: no .agentic/ in {project_dir}", file=sys.stderr)
        return 1

    if not getattr(args, "open", False):
        url = dashboard_open._live_url(project_dir)
        print(url or "dashboard: no live server (start a pipeline first)")
        return 0

    # An explicit open may be the first call of the day: bring the server up.
    try:
        port, _server = start_dashboard_with_reuse(project_dir)
        print(f"dashboard: http://127.0.0.1:{port}")
    except Exception as e:  # noqa: BLE001 — fall back to the static page
        print(f"dashboard: server unavailable ({e}); opening the static page")

    result = dashboard_open.open_dashboard(project_dir, explicit=True)
    if result.get("already_open"):
        print(f"already open: {result.get('url')}")
        return 0
    if not result.get("opened"):
        print("dashboard: could not open a page", file=sys.stderr)
        return 1
    print(f"opened: {result.get('url')}")
    return 0
