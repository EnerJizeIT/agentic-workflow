"""Localhost HTTP server for accepting form submits.

Runs in a daemon thread, started at plugin startup. Listens on 127.0.0.1 only.
"""
from __future__ import annotations

import html
import json
import logging
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
    """Check if form_id looks valid (FORM-* prefix, reasonable length).

    Defense-in-depth against path traversal: form_id builds the submit file
    path (``inputs_dir / f"{form_id}.yaml"``). The registry lookup is the
    primary guard (IDs are server-generated), but reject path separators and
    ``..`` here too so a crafted ID can never escape inputs_dir even if a
    future code path bypasses the registry check.
    """
    if not form_id.startswith("FORM-") or not (6 < len(form_id) < 100):
        return False
    # P1: ASCII-only (prevents unicode filenames in inputs_dir)
    if not form_id.isascii():
        return False
    # Legit IDs are FORM-<alphanumeric/hyphen> only.
    if "/" in form_id or "\\" in form_id or ".." in form_id:
        return False
    return True


def _is_origin_allowed(origin: str, referer: str) -> bool:
    """A2/KAUD-3: CSRF check — is this Origin/Referer combination allowed?

    Uses urlparse to check hostname, not startswith (which allowed bypass
    via http://127.0.0.1.evil.com).

    Forms opened via file:// (opencode temp HTML) send Origin: "null"
    (browser standard for sandboxed/local file origins).
    """
    from urllib.parse import urlparse

    ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

    def _check_url(url: str) -> bool:
        if not url:
            return False
        try:
            parsed = urlparse(url)
            return parsed.hostname in ALLOWED_HOSTS
        except (ValueError, TypeError):
            return False

    # Origin "null" — local file:// page or sandboxed iframe. Server is
    # 127.0.0.1-only, so this is always safe.
    if origin == "null":
        return True
    if origin:
        return _check_url(origin)
    # No Origin header — check Referer if present (curl/non-browser has neither)
    if referer:
        if referer.startswith("file://"):
            return True
        return _check_url(referer)
    return True  # no Origin, no Referer — backward compat for curl


def _ack_page(form_id: str, already_submitted: bool) -> str:
    """Generate HTML acknowledgement page shown after submit.

    A3: rendered from render/default_templates/ack.html.j2 (was inline f-string).
    """
    from .render.engine import render_template
    from .state import get_jinja_env

    message = "Эта форма уже была отправлена ранее." if already_submitted else "Форма отправлена!"
    try:
        env = get_jinja_env()
        return render_template(env, "ack", {
            "form_id": form_id,
            "message": message,
        })
    except Exception as e:
        # Fallback: minimal HTML if template engine fails
        return (
            f"<!DOCTYPE html><html><body>"
            f"<h1>{html.escape(message)}</h1>"
            f"<p>ID: {html.escape(form_id)}</p>"
            f"<!-- template render failed: {html.escape(str(e))} -->"
            f"</body></html>"
        )


class SubmitHandler(BaseHTTPRequestHandler):
    """HTTP request handler for /submit/<form_id> and /health."""

    # Injected via handler_class factory (see _make_handler_class)
    inputs_dir: Path = None  # type: ignore[assignment]
    registry: FormRegistry = None  # type: ignore[assignment]

    def do_POST(self):
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

        # A2: CSRF protection — verify Origin/Referer is localhost or local file.
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        if not _is_origin_allowed(origin, referer):
            self._send_text(403, f"Forbidden: Origin '{origin}' not allowed")
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_text(400, "Invalid Content-Length header")
            return
        if content_length > MAX_BODY_BYTES:
            # Drain request body before responding, otherwise client gets
            # BrokenPipeError when server closes connection mid-write.
            remaining = content_length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
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

        inputs_dir = (record.project_dir / ".agentic" / "inputs") if record.project_dir else self.inputs_dir
        inputs_dir.mkdir(parents=True, exist_ok=True)
        target = inputs_dir / f"{form_id}.yaml"
        # H4 fix: atomic check-and-set — claim the form before writing.
        # Without this, two parallel POSTs could both pass the status check.
        if not self.registry.claim_for_submit(form_id):
            self._send_text(409, f"Form {form_id} is being submitted by another request or already processed.")
            return

        # QA-FIX: if _atomic_write_yaml fails (disk full, permission, etc.),
        # rollback form status to "pending" — otherwise it stays stuck in
        # "submitting" forever and user can't resubmit (claim_for_submit
        # rejects anything != "pending"). finalize_submit below only runs
        # on success.
        try:
            _atomic_write_yaml(target, payload)
            import os as _os
            _os.chmod(target, 0o600)
        except OSError as e:
            self.registry.update_status(form_id, "pending")
            log.error("Failed to write submit file %s: %s", target, e)
            self._send_text(500, "Server error: failed to persist submit. Please retry.")
            return

        # H4 fix: finalize submit status
        self.registry.finalize_submit(form_id)

        # Persist custom roles if requested (delegates to roles_processor)
        # QA-D: only project-setup submit has role fields. Other templates
        # (increment-planning, etc.) called process_role_saves/deletions as
        # no-op but wasted cycles + log noise. Gate explicitly.
        from .roles_processor import process_role_deletions, process_role_saves

        if record.template == "project-setup":
            process_role_saves(data, project_dir=record.project_dir)
            process_role_deletions(data, project_dir=record.project_dir)

        # Dogfood-7: increment-planning submit → persist via api.apply_increment_plan
        if record.template == "increment-planning" and record.project_dir:
            selected_variant_id = (data.get("selected_variant", "") or "").strip()
            variants_json = data.get("variants_json", "") or "[]"
            if selected_variant_id and selected_variant_id != "__reject__":
                try:
                    import json as _json
                    variants = _json.loads(variants_json) if isinstance(variants_json, str) else variants_json
                    selected = next(
                        (v for v in variants if isinstance(v, dict) and v.get("id") == selected_variant_id),
                        None,
                    )
                    if selected:
                        # Build plan.md body from selected variant
                        plan_body = _build_plan_md_from_variant(selected, variants)
                        from awf.api import apply_increment_plan
                        apply_increment_plan(
                            record.project_dir,
                            plan_body,
                            selected_variant_id=selected_variant_id,
                            variants=variants,
                        )
                        log.info(
                            "Increment plan persisted: variant=%s → plan.md",
                            selected_variant_id,
                        )
                except Exception as e:
                    log.error("apply_increment_plan failed: %s", e)

        log.info("Submit received for %s, written to %s", form_id, target)

        self._send_html(200, _ack_page(form_id, already_submitted=False))


    def do_GET(self):
        """Handle GET /health."""
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        self._send_text(404, "Not Found")

    # Silence default logging to stdout (would corrupt MCP stdio protocol if it leaked)
    def log_message(self, format, *args):
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



def _build_plan_md_from_variant(
    selected: dict[str, Any],
    all_variants: list[dict[str, Any]],
) -> str:
    """Build plan.md body from user-selected increment variant.

    Supervisor's variant carries the decomposition. We render it as
    markdown so plan.md is human-readable and supervisor can edit later.
    """
    title = selected.get("title", "Untitled plan")
    strategy = selected.get("strategy", "")
    description = selected.get("description", "")
    increments = selected.get("increments", []) or []
    pros = selected.get("pros", []) or []
    cons = selected.get("cons", []) or []
    est_todos = selected.get("estimated_todos")
    est_time = selected.get("estimated_time")

    lines: list[str] = [f"# Plan: {title}", ""]
    if strategy:
        lines.append(f"**Strategy:** {strategy}")
    if description:
        lines.append("")
        lines.append(description)
    lines.append("")
    if est_todos or est_time:
        meta_parts = []
        if est_todos:
            meta_parts.append(f"~{est_todos} TODOs")
        if est_time:
            meta_parts.append(f"~{est_time}")
        lines.append(f"_Estimated: {', '.join(meta_parts)}_")
        lines.append("")

    lines.append("## Increments")
    lines.append("")
    for i, inc in enumerate(increments, start=1):
        name = inc.get("name", f"Increment {i}")
        goal = inc.get("goal", "")
        artefacts = inc.get("artefacts", []) or []
        lines.append(f"### {i}. {name}")
        if goal:
            lines.append("")
            lines.append(goal)
        if artefacts:
            lines.append("")
            lines.append(f"**Artefacts:** {', '.join(artefacts)}")
        lines.append("")

    if pros or cons:
        lines.append("## Trade-offs")
        lines.append("")
        if pros:
            lines.append("**Pros:**")
            for p in pros:
                lines.append(f"- {p}")
            lines.append("")
        if cons:
            lines.append("**Cons:**")
            for c in cons:
                lines.append(f"- {c}")
            lines.append("")

    # Other variants as historical reference
    if len(all_variants) > 1:
        other = [v for v in all_variants if v.get("id") != selected.get("id")]
        lines.append("## Other variants considered")
        lines.append("")
        for v in other:
            lines.append(f"- **{v.get('id', '?')}**: {v.get('title', 'untitled')}")
        lines.append("")

    return "\n".join(lines)
