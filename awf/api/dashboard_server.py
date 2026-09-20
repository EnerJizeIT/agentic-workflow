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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return actual_port, server


__all__ = ["start_dashboard_server"]
