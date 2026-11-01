"""Dashboard opening — one rule for the engine and the CLI.

The dashboard server lives at a per-project port (``state/dashboard_port``)
that survives restarts, so a browser tab keeps its URL across
start/continue/run_next. The marker ``context/dashboard_opened`` records the
last URL a tab was opened for; both surfaces below read it, so the engine's
automatic open and a manual ``awf dashboard --open`` never fight.

Two policies, one file:

* **automatic** (pipeline launch): open only when no tab was opened for the
  CURRENT url, whatever its age — a long run must not spawn a second tab
  when the first is still looking at the same port.
* **explicit** (``awf dashboard --open``): the owner asked, so a short
  window (``EXPLICIT_DEDUP_SECONDS``) protects against a double click, but
  the tab can be reopened after that — the owner may have closed it.

The file:// fallback is the same page the plugin uses when the server is
gone: a dashboard nobody can start is worse than a static one.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

#: A double click on "open dashboard" must not open two tabs; after that the
#: owner may legitimately have closed the tab and want it back.
EXPLICIT_DEDUP_SECONDS = 60.0


def _marker_file(project_dir: Path) -> Path:
    return project_dir / ".agentic" / "context" / "dashboard_opened"


def _read_marker(project_dir: Path) -> dict:
    try:
        data = json.loads(_marker_file(project_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(project_dir: Path, url: str) -> None:
    path = _marker_file(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"ts": time.time(), "url": url}), encoding="utf-8"
        )
    except OSError:
        # A marker write failure must never break the open itself.
        pass


def _port_alive(port: int, timeout: float = 0.25) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _live_url(project_dir: Path) -> str:
    """``http://127.0.0.1:<port>`` when the server answers, else ``""``."""
    from .. import paths

    port_file = paths.agentic_dir(project_dir) / "state" / "dashboard_port"
    try:
        port = int(port_file.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return ""
    if not port or not _port_alive(port):
        return ""
    return f"http://127.0.0.1:{port}"


def should_open(project_dir: Path, url: str, *, explicit: bool) -> bool:
    """Whether a tab for ``url`` belongs to be opened right now.

    Automatic: no second tab for the same url, ever. Explicit: a short
    window only, so a closed tab can be reopened on request.
    """
    marker = _read_marker(project_dir)
    if str(marker.get("url", "")) != url:
        return True
    if explicit:
        try:
            age = time.time() - float(marker.get("ts", 0))
        except (TypeError, ValueError):
            return True
        return age > EXPLICIT_DEDUP_SECONDS
    return False


def open_dashboard(
    project_dir: str | Path,
    *,
    explicit: bool = False,
    browser_open=None,
) -> dict:
    """Open the dashboard; returns ``{opened, url, method, already_open}``.

    Never raises: a missing server falls back to the generated page, and a
    missing page is an honest ``opened: False``.
    """
    from .. import paths

    project = Path(project_dir).resolve()
    opener = browser_open or _default_browser_open

    url = _live_url(project)
    method = "http"
    if not url:
        from .dashboard import generate_dashboard

        try:
            generate_dashboard(project)
        except Exception:  # noqa: BLE001 — a dashboard is a convenience
            return {"opened": False, "url": "", "method": "none"}
        page = paths.agentic_dir(project) / "dashboards" / "current.html"
        if not page.is_file():
            return {"opened": False, "url": "", "method": "none"}
        url, method = f"file://{page}", "file"

    if not should_open(project, url, explicit=explicit):
        return {
            "opened": False,
            "already_open": True,
            "url": url,
            "method": method,
        }

    try:
        opener(url)
    except Exception:  # noqa: BLE001 — no browser on a headless box
        return {"opened": False, "url": url, "method": method}
    _write_marker(project, url)
    return {"opened": True, "url": url, "method": method}


def _default_browser_open(url: str) -> None:
    import webbrowser

    webbrowser.open(url)


def gui_available() -> bool:
    """Whether a browser window may be opened here.

    Three switches, each with a story:

    * ``CI`` — a headless runner has no browser;
    * ``PYTEST_CURRENT_TEST`` — the suite spawns hundreds of pipelines in
      tmp projects (01.11: every one of them opened a tab in the owner's
      Chrome and Firefox); a test process is never a user session;
    * ``AWF_NO_DASHBOARD_OPEN`` — an explicit "not now" from the owner.

    A graphical session is required on top: ``DISPLAY`` or ``WAYLAND_DISPLAY``.
    """
    import os

    for name in ("CI", "PYTEST_CURRENT_TEST", "AWF_NO_DASHBOARD_OPEN"):
        if os.environ.get(name, "").strip():
            return False
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


__all__ = [
    "EXPLICIT_DEDUP_SECONDS",
    "gui_available",
    "open_dashboard",
    "should_open",
]
