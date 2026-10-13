"""Dashboard HTTP server — serves HTML + JSON state for live updates.

Started by orchestrator in a daemon thread. Eliminates file:// reload
problem: JavaScript polls /api/state every 3s and patches DOM smoothly.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import paths


class _Flight:
    """One in-flight generate_state_dict; concurrent polls join it."""

    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict | None = None
        self.error: BaseException | None = None


class _StateCoalescer:
    """AUD15-02: collapse concurrent /api/state polls into ONE computation.

    ThreadingHTTPServer spawns a thread per request; with a CPU-bound
    generate_state_dict the N concurrent polls ran N full parses under the
    GIL (super-linear collapse). Now: a poll that finds no fresh cache
    entry either becomes the single computation leader or joins the leader
    via an Event. A short TTL additionally serves rapid repeats from cache.
    """

    def __init__(self, ttl: float = 1.0, wait_timeout: float = 120.0) -> None:
        self._ttl = ttl
        self._wait_timeout = wait_timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, dict]] = {}
        self._flights: dict[str, _Flight] = {}

    def get(self, key: str, compute) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = _Flight()
                self._flights[key] = flight
        assert flight is not None
        if leader:
            try:
                result = compute()
                flight.result = result
                with self._lock:
                    self._cache[key] = (time.monotonic(), result)
            except BaseException as e:  # followers must see the same failure
                flight.error = e
            finally:
                flight.done.set()
                with self._lock:
                    if self._flights.get(key) is flight:
                        del self._flights[key]
        else:
            flight.done.wait(timeout=self._wait_timeout)
        if flight.error is not None:
            raise flight.error
        if flight.result is None:
            raise RuntimeError("state computation lost its result")
        return flight.result


# REPORTS33 (TODO-0210): the RUN-REPORT name the engine writes
# (awf/api/run.py::_write_report — strftime %Y%m%d-%H%M%S). The strict
# pattern is the traversal gate: anything else (../, foreign names,
# URL-encoded dots) is refused before a path is even built.
_RUN_REPORT_NAME_RE = re.compile(r"^RUN-REPORT-\d{8}-\d{6}\.md$")
_RUN_REPORT_TS_RE = re.compile(r"^RUN-REPORT-(\d{8})-(\d{6})\.md$")
_QUEUE_DONE_RE = re.compile(r"(\d+)/(\d+)\s+done")
_REASON_RE = re.compile(r"^\*\*Reason:\*\*\s*(.+)$", flags=re.MULTILINE)


def _run_report_summary(f: Path) -> dict[str, str] | None:
    """One completed run for the /api/runs history: date + reason +
    queue progress, parsed from the report head (the engine writes both
    lines in the first 10 lines; a truncated report degrades to blanks)."""
    m = _RUN_REPORT_TS_RE.match(f.name)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    try:
        head = f.open(encoding="utf-8", errors="replace").read(4096)
    except OSError:
        head = ""
    reason = ""
    rm = _REASON_RE.search(head)
    if rm:
        reason = rm.group(1).strip()
    queue = ""
    qm = _QUEUE_DONE_RE.search(head)
    if qm:
        queue = f"{qm.group(1)}/{qm.group(2)} done"
    return {
        "name": f.name,
        "date": dt.strftime("%Y-%m-%d %H:%M"),
        "reason": reason,
        "queue": queue,
    }


def build_runs_dict(project_dir: Path) -> dict[str, Any]:
    """REPORTS33 (TODO-0210): /api/runs payload — the active run + the
    completed runs (RUN-REPORT-*.md in the outbox, newest first).

    Read-only, best-effort: a missing/corrupt run state degrades to
    active=None, an unreadable report drops out of the history — the
    dashboard never 500s on report files.
    """
    active: dict[str, Any] | None = None
    try:
        from .run import run_brief

        brief = run_brief(project_dir)
    except Exception:
        brief = None
    if brief and brief.get("active"):
        active = {
            "position": str(brief.get("position") or ""),
            "current": str(brief.get("current") or ""),
            "note": str(brief.get("note") or ""),
        }
    history: list[dict[str, str]] = []
    outbox = paths.outbox(project_dir)
    if outbox.is_dir():
        try:
            files = sorted(outbox.glob("RUN-REPORT-*.md"), reverse=True)
        except OSError:
            files = []
        for f in files:
            if not f.is_file():
                continue
            entry = _run_report_summary(f)
            if entry is not None:
                history.append(entry)
    return {"active": active, "history": history}


def _render_report_html(content: str) -> str:
    """REPORTS33 (TODO-0210): report markdown → dashboard-safe HTML.

    The same chain as the handoffs (awf/api/dashboard.py): the goal/
    criteria lines are USER text, so escape first, then markdown, then
    neutralize executable hrefs. A rendering failure degrades to ""
    (the page shows the raw text in a pre block).
    """
    import html as _html

    try:
        import markdown as _md

        from .dashboard import _neutralize_unsafe_links

        return _neutralize_unsafe_links(
            _md.markdown(_html.escape(content), extensions=["fenced_code"])
        )
    except Exception:
        return ""


class _DashboardHandler(BaseHTTPRequestHandler):
    """Serves dashboard HTML at / and JSON state at /api/state."""

    def do_GET(self) -> None:
        # urlsplit: the query string never reaches the route checks
        path = urlsplit(self.path).path
        if path.startswith("/api/state"):
            self._serve_state()
        elif path == "/api/runs":
            self._serve_runs()
        elif path.startswith("/api/run-report/"):
            self._serve_run_report(path[len("/api/run-report/"):])
        elif path == "/" or path == "/index.html":
            self._serve_html()
        else:
            self.send_error(404)

    def _send_json(self, status: int, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_runs(self) -> None:
        self._send_json(200, build_runs_dict(self.server.project_dir))

    def _serve_run_report(self, raw_name: str) -> None:
        """REPORTS33 (TODO-0210): one RUN-REPORT from the outbox —
        read-only, the name is regex-gated and the resolved path must
        stay inside .agentic/outbox (no ../, no foreign files)."""
        name = raw_name.strip()
        if not _RUN_REPORT_NAME_RE.match(name):
            self._send_json(404, {"error": "invalid run report name"})
            return
        outbox = paths.outbox(self.server.project_dir)
        try:
            resolved = (outbox / name).resolve()
            if outbox.resolve() not in resolved.parents:
                self._send_json(404, {"error": "invalid run report name"})
                return
        except OSError:
            self._send_json(404, {"error": "invalid run report name"})
            return
        if not resolved.is_file():
            self._send_json(404, {"error": "run report not found"})
            return
        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._send_json(404, {"error": "run report not found"})
            return
        self._send_json(
            200,
            {"name": name, "content": content, "html": _render_report_html(content)},
        )

    def _serve_html(self) -> None:
        html_path = self.server.project_dir / ".agentic" / "dashboards" / "current.html"
        if not html_path.is_file():
            self.send_error(404, "Dashboard not generated yet")
            return
        data = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_state(self) -> None:
        from .dashboard import generate_state_dict

        # AUD15-02: one computation per burst of concurrent polls (the
        # coalescer is per-server, created in start_dashboard_server).
        key = str(self.server.project_dir)
        coalescer = self.server._state_coalescer
        try:
            state = coalescer.get(
                key, lambda: generate_state_dict(self.server.project_dir)
            )
            data = json.dumps(state, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            # Day-3: no Access-Control-Allow-Origin — the dashboard page is
            # served from this same origin; a wildcard let ANY web page read
            # the pipeline state from localhost.
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            error = json.dumps({"error": str(e)}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(error)))
            self.end_headers()
            self.wfile.write(error)

    def log_message(self, *args: object) -> None:
        pass  # suppress request logging


def start_dashboard_server(
    project_dir: Path, port: int = 0,
) -> tuple[int, ThreadingHTTPServer]:
    """Start dashboard HTTP server. Returns (port, server).

    Server runs in a daemon thread. Stops automatically when main process exits.
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), _DashboardHandler)
    server.project_dir = project_dir  # type: ignore[attr-defined]
    server._state_coalescer = _StateCoalescer()  # type: ignore[attr-defined]
    actual_port = server.server_address[1]
    # REPORTS29: fast poll so the exit-path stop (stop_dashboard_server)
    # returns promptly — the 0.5s default would stretch the pipeline
    # epilogue and race the next in-process launch ("Pipeline already
    # running" false refusal, seam matrix).
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
    )
    thread.start()
    return actual_port, server


def _port_file(project_dir: Path) -> Path:
    return paths.agentic_dir(project_dir) / "state" / "dashboard_port"


def _read_reuse_port(project_dir: Path) -> int:
    """The previous launch's port (0 when absent/garbage/out of range)."""
    try:
        if _port_file(project_dir).is_file():
            port = int(_port_file(project_dir).read_text(encoding="utf-8").strip() or 0)
            return port if 0 < port <= 65535 else 0
    except (OSError, ValueError):
        pass
    return 0


def _boot_marker_file(project_dir: Path) -> Path:
    return paths.agentic_dir(project_dir) / "state" / "dashboard_boot"


def _write_boot_marker(project_dir: Path) -> None:
    """REPORTS30: write the actual-start marker (pid + ts).

    Callers: only ``start_dashboard_with_reuse`` on a REAL start (a new
    port). The marker survives exit, exactly like the port file: the next
    launch's plugin reads it to decide whether THIS launch started the
    server fresh (open a tab) or merely reused the previous port (a live
    tab already points at the same URL — do not open a second one).
    """
    f = _boot_marker_file(project_dir)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}),
        encoding="utf-8",
    )


def start_dashboard_with_reuse(
    project_dir: Path,
) -> tuple[int, ThreadingHTTPServer]:
    """Start the dashboard server, reusing the previous launch's port (REPORTS29).

    state/dashboard_port is the reuse token: it survives EVERY exit (clean
    or killed), so consecutive launches (start/continue/run_next) rebind the
    same port and the browser tab keeps its URL. A dead own server leaves
    TIME_WAIT sockets — SO_REUSEADDR (set by HTTPServer by default) rebinds
    over them. A port held by a FOREIGN process is a bind failure → an
    honest fallback to a random port, and the file is rewritten with it
    (only on a real change).

    REPORTS30: a real start (``port != prev_port``) also writes the boot
    marker state/dashboard_boot — BEFORE the port file, so a poller that
    sees the port file always sees the marker too. On a port reuse the
    marker is NOT updated: it stays with the last actual start.
    """
    prev_port = _read_reuse_port(project_dir)
    try:
        port, server = start_dashboard_server(project_dir, port=prev_port)
    except OSError:
        port, server = start_dashboard_server(project_dir)
    if port != prev_port:
        _write_boot_marker(project_dir)
        f = _port_file(project_dir)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(str(port), encoding="utf-8")
    return port, server


def stop_dashboard_server(server: ThreadingHTTPServer | None) -> None:
    """Stop a dashboard server on the exit path (REPORTS29).

    In a real launch the process death stops the daemon thread; explicit
    stop matters for in-process consecutive launches (MCP continue, tests),
    which must rebind the port. The port file is NOT touched here — it is
    the reuse token for the next launch (a dead port is harmless: the
    open-dashboard path liveness-checks before opening a URL).
    """
    if server is None:
        return
    try:
        server.shutdown()
        server.server_close()
    except OSError:
        pass


__all__ = [
    "start_dashboard_server",
    "start_dashboard_with_reuse",
    "stop_dashboard_server",
]
