"""Dashboard HTTP server — serves HTML + JSON state for live updates.

Started by orchestrator in a daemon thread. Eliminates file:// reload
problem: JavaScript polls /api/state every 3s and patches DOM smoothly.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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

        try:
            state = generate_state_dict(self.server.project_dir)
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
    actual_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return actual_port, server


__all__ = ["start_dashboard_server"]
