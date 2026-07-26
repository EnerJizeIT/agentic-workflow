"""Localhost HTTP server for accepting form submits.

Runs in a daemon thread, started at plugin startup. Listens on 127.0.0.1 only.
"""
from __future__ import annotations

import html
import json
import logging
import re
import socket
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config
from .state import FormRegistry

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1MB


def _find_free_port() -> int:
    """Ask OS for a free port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _atomic_write_yaml(path: Path, data: dict[str, Any]) -> None:
    """Write YAML atomically (temp file + rename)."""
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{uuid4().hex}.tmp")
    content = yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _is_valid_form_id(form_id: str) -> bool:
    """Check if form_id looks valid (FORM-* prefix, reasonable length)."""
    return form_id.startswith("FORM-") and 6 < len(form_id) < 100


def _ack_page(form_id: str, already_submitted: bool) -> str:
    """Generate HTML acknowledgement page shown after submit."""
    message = "Already submitted earlier." if already_submitted else "Submitted successfully!"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Submit acknowledgement</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 500px; margin: 80px auto; padding: 0 20px; text-align: center; color: #333; }}
    h1 {{ color: #2e7d32; margin-bottom: 8px; }}
    .form-id {{ font-family: monospace; background: #f5f5f5; padding: 8px 16px; border-radius: 4px; display: inline-block; margin: 16px 0; }}
    .next-steps {{ margin-top: 32px; padding: 20px; background: #e3f2fd; border-radius: 8px; text-align: left; }}
    .next-steps h2 {{ font-size: 16px; margin: 0 0 12px 0; color: #1565c0; }}
    .next-steps ol {{ margin: 0; padding-left: 20px; }}
    .next-steps li {{ margin-bottom: 8px; }}
    .next-steps code {{ background: #fff; padding: 2px 6px; border-radius: 3px; font-family: monospace; }}
  </style>
</head>
<body>
  <h1>{html.escape(message)}</h1>
  <p>Form ID:</p>
  <div class="form-id">{html.escape(form_id)}</div>

  <div class="next-steps">
    <h2>⚠️ Next step — go back to your CLI</h2>
    <ol>
      <li>Switch to your <strong>opencode CLI</strong> terminal.</li>
      <li>Type a message to the agent, e.g.: <code>done</code> or <code>I submitted the form</code>.</li>
      <li>The agent will read your submission and continue.</li>
    </ol>
    <p style="margin: 12px 0 0 0; font-size: 14px; color: #666;">
      The agent is <strong>not blocked</strong> waiting for you — it continues working.
      When you tell it you're done, it will pick up your form data.
    </p>
  </div>
</body>
</html>
"""


class SubmitHandler(BaseHTTPRequestHandler):
    """HTTP request handler for /submit/<form_id> and /health."""

    # Injected via handler_class factory (see _make_handler_class)
    inputs_dir: Path = None  # type: ignore[assignment]
    registry: FormRegistry = None  # type: ignore[assignment]

    def do_POST(self):  # noqa: N802 - http.server API
        """Handle POST /submit/<form_id>."""
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/submit/"):
            self._send_text(404, "Not Found")
            return

        form_id = path[len("/submit/"):]
        if not _is_valid_form_id(form_id):
            self._send_text(404, f"Invalid form_id: {html.escape(form_id)}")
            return

        record = self.registry.get(form_id)
        if record is None:
            self._send_text(404, f"Form not found: {html.escape(form_id)}")
            return

        if record.status == "submitted":
            self._send_html(200, _ack_page(form_id, already_submitted=True))
            return

        if record.status == "cancelled":
            self._send_text(410, f"Form {form_id} was cancelled.")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_BYTES:
            self._send_text(413, "Payload Too Large (max 1MB)")
            return
        if content_length == 0:
            self._send_text(400, "Empty body")
            return

        body = self.rfile.read(content_length).decode("utf-8", errors="replace")
        parsed = urllib.parse.parse_qs(body, keep_blank_values=True)
        data: dict[str, Any] = {}
        for key, values in parsed.items():
            if len(values) == 1:
                data[key] = values[0]
            else:
                data[key] = values

        payload = {
            "form_id": form_id,
            "template": record.template,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }

        target = self.inputs_dir / f"{form_id}.yaml"
        _atomic_write_yaml(target, payload)

        self.registry.update_status(form_id, "submitted")

        log.info("Submit received for %s, written to %s", form_id, target)

        self._send_html(200, _ack_page(form_id, already_submitted=False))

    def do_GET(self):  # noqa: N802 - http.server API
        """Handle GET /health."""
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_text(404, "Not Found")

    # Silence default logging to stdout (would corrupt MCP stdio protocol if it leaked)
    def log_message(self, format, *args):  # noqa: A002, D401
        log.debug("HTTP %s - %s", self.address_string(), format % args)

    # Helpers
    def _send_html(self, code: int, body: str) -> None:
        body_bytes = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_text(self, code: int, body: str) -> None:
        body_bytes = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_handler_class(inputs_dir: Path, registry: FormRegistry):
    """Create a handler class with bound inputs_dir and registry."""
    cls = type(
        "BoundSubmitHandler",
        (SubmitHandler,),
        {"inputs_dir": inputs_dir, "registry": registry},
    )
    return cls


def start_http_server(config: Config, registry: FormRegistry) -> tuple[ThreadingHTTPServer, int]:
    """Start HTTP server in background thread.

    Args:
        config: Plugin config (provides port, inputs_dir).
        registry: Form registry (for form_id validation).

    Returns:
        Tuple (server, actual_port). actual_port == config.http_port if non-zero, else auto-selected.
    """
    port = config.http_port if config.http_port > 0 else _find_free_port()
    handler_class = _make_handler_class(config.inputs_dir, registry)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="awf-ui-http")
    thread.start()
    log.info("HTTP server listening on http://127.0.0.1:%d", port)
    return server, port
