"""Dashboard HTTP server — serves HTML + JSON state for live updates.

Started by orchestrator in a daemon thread. Eliminates file:// reload
problem: JavaScript polls /api/state every 3s and patches DOM smoothly.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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


class _DashboardHandler(BaseHTTPRequestHandler):
    """Serves dashboard HTML at / and JSON state at /api/state."""

    def do_GET(self) -> None:
        if self.path.startswith("/api/state"):
            self._serve_state()
        elif self.path == "/" or self.path == "/index.html":
            self._serve_html()
        else:
            self.send_error(404)

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
    """
    prev_port = _read_reuse_port(project_dir)
    try:
        port, server = start_dashboard_server(project_dir, port=prev_port)
    except OSError:
        port, server = start_dashboard_server(project_dir)
    if port != prev_port:
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
