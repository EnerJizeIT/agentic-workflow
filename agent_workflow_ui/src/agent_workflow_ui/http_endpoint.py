"""Localhost HTTP server for accepting form submits.

Runs in a daemon thread, started at plugin startup. Listens on 127.0.0.1 only.
"""
from __future__ import annotations

import html
import json
import logging
import os
import socket
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Config
from .state import FormRecord, FormRegistry

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 1 * 1024 * 1024  # 1MB
# A-07 (аудит 2026-09-25, слой 5): пределы обработки тела POST формы —
# та же форма, что у ядровой половины (awf/plan_checkpoint.py: 64 KiB,
# 10 c, кап 16). Для форм лимит тела остаётся 1 MiB.
BODY_READ_TIMEOUT = 10.0  # секунд простоя сокета на чтении
MAX_CONCURRENT_HANDLERS = 16  # максимум одновременных обработчиков


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
    # AUD14-06e: submit file (may carry form payload) — create tmp 0600
    # before the rename instead of the old write_text(0644) + chmod-after.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
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


# A-05: per-form locks for resubmit re-apply (in-process exactly-once;
# the submit-file re-check under the lock is the real guard).
_REAPPLY_LOCKS: dict[str, threading.Lock] = {}
_REAPPLY_LOCKS_GUARD = threading.Lock()


def _reapply_lock(form_id: str) -> threading.Lock:
    with _REAPPLY_LOCKS_GUARD:
        lock = _REAPPLY_LOCKS.get(form_id)
        if lock is None:
            lock = threading.Lock()
            _REAPPLY_LOCKS[form_id] = lock
        return lock


@dataclass
class ApplyResult:
    """A-05: outcome of a submit's materialization.

    ``ok`` — no part failed. ``applied``/``errors``/``warnings`` are the
    explicit partial report (invariant 3): what landed, what did not.
    """

    ok: bool
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _ack_message(already_submitted: bool, apply: dict[str, Any] | None) -> str:
    """A-05: the ack text distinguishes received / applied / failed."""
    if apply is None:
        return "Эта форма уже была отправлена ранее." if already_submitted else "Форма отправлена!"
    if apply.get("ok"):
        if already_submitted:
            return "Форма уже применена." if apply.get("applied") else "Эта форма уже была отправлена ранее."
        return "Форма применена!" if apply.get("applied") else "Форма отправлена!"
    errors = apply.get("errors") or ["unknown error"]
    first = str(errors[0])
    if len(first) > 200:
        first = first[:200] + "…"
    return f"Форма получена. Применение не удалось: {first}. Повторите отправку — применение повторится."


def _ack_page(form_id: str, already_submitted: bool = False, apply: dict[str, Any] | None = None) -> str:
    """Generate HTML acknowledgement page shown after submit.

    A3: rendered from render/default_templates/ack.html.j2 (was inline f-string).
    A-05: ``apply`` (the materialization outcome) selects the message.
    """
    from .render.engine import render_template
    from .state import get_jinja_env

    message = _ack_message(already_submitted, apply)
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

    def setup(self) -> None:
        super().setup()
        # A-07: любое чтение на этом соединении (request line, тело)
        # ограничено по времени — зависший клиент не удерживает поток
        # обработчика. socket.timeout на request line уже обработан
        # BaseHTTPRequestHandler (закрытие без traceback).
        self.connection.settimeout(BODY_READ_TIMEOUT)

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
            # A-05: resubmit either reports the completed apply or completes
            # it from the saved payload (exactly once — the applied marker
            # is not doubled). Legacy files keep the old ack text.
            apply_info = self._resubmit_apply_info(form_id, record)
            self._send_html(200, _ack_page(form_id, already_submitted=True, apply=apply_info))
            return

        if record.status == "cancelled":
            self._send_text(410, f"Form {form_id} was cancelled.")
            return

        # AUD09-01: TTL is enforced here, not only lazily in read_submit/
        # list_pending — a stale form submitted from the browser must not
        # materialize. Expired (or already flipped to "expired") → 410.
        if record.status == "expired" or (
            record.expires_at is not None
            and datetime.now(timezone.utc) > record.expires_at
        ):
            if record.status != "expired":
                self.registry.update_status(form_id, "expired")
            self._send_text(410, f"Form {form_id} expired.")
            return

        # A2: CSRF protection — verify Origin/Referer is localhost or local file.
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        if not _is_origin_allowed(origin, referer):
            self._send_text(403, f"Forbidden: Origin '{origin}' not allowed")
            return

        # A-07: Content-Length валидируется до любого чтения: мусор или
        # отрицательное число — немедленный 400, тело не читается, форма
        # не трогается.
        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._send_text(400, "Invalid Content-Length header")
            return
        # AUD09-07: a negative length is never legitimate.
        if content_length < 0:
            self._send_text(400, "Invalid Content-Length header")
            return
        if content_length > MAX_BODY_BYTES:
            # A-07: перелив — 413 без вычитывания тела: соединение
            # закрывается, поток освобождается, заявленные мегабайты не
            # читаются в память. Старый drain висел на частичной отправке.
            self._send_text(413, "Payload Too Large (max 1MB)", close=True)
            return
        if content_length == 0:
            self._send_text(400, "Empty body")
            return

        # A-07: чтение ограничено по времени (socket timeout выставляется
        # в setup) — медленный или зависший клиент не держит поток.
        try:
            body_bytes = self.rfile.read(content_length)
        except OSError:  # TimeoutError ⊂ OSError (py3.10+) — read timeout
            self._send_text(408, "Timeout reading body", close=True)
            return
        finally:
            # Таймаут был только для чтения — ответ ограничивать не нужно.
            try:
                self.connection.settimeout(None)
            except OSError:
                pass
        # AUD09-07: if the connection closed before CL bytes arrived the
        # body is truncated. Continue would "submit" a partial/empty payload
        # and burn the pending form — reject instead.
        if len(body_bytes) != content_length:
            self._send_text(400, "Short body: connection closed mid-request")
            return
        body = body_bytes.decode("utf-8", errors="replace")
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
            # A-05: additive. "received" until the materialization below
            # flips it to applied/failed (a crash in between leaves the
            # explicit received state, and a resubmit completes the apply).
            "apply_status": "received",
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

        # A-05: run the materialization (project-setup → roles +
        # apply_project_setup via roles_processor; increment-planning →
        # apply_increment_plan) and record its outcome in the submit file.
        # The form status now distinguishes received from applied/failed;
        # the result (success/error, what was applied) is returned via
        # read_submit and the ack page.
        apply_result = apply_form_submit(record.template, data, record.project_dir)
        apply_record = {
            **apply_result.to_dict(),
            "applied_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 1,
        }
        payload["apply_status"] = "applied" if apply_result.ok else "failed"
        payload["apply_result"] = apply_record
        try:
            _atomic_write_yaml(target, payload)
            os.chmod(target, 0o600)
        except OSError as e:
            # The apply already ran; the file keeps "received". A resubmit
            # re-runs the apply (idempotent) and records it.
            log.error("Failed to record apply result for %s: %s", target, e)

        log.info("Submit received for %s, written to %s (apply: %s)", form_id, target, payload["apply_status"])

        self._send_html(200, _ack_page(form_id, already_submitted=False, apply=apply_record))


    def _resubmit_apply_info(self, form_id: str, record: FormRecord) -> dict[str, Any] | None:
        """A-05: apply info for a resubmit of an already-submitted form.

        Returns the stored apply result when the form is already applied,
        re-runs the apply from the SAVED payload when it failed (or is
        still "received" after a crash) and records the new result, or
        None for a legacy file written before apply tracking existed
        (the old ack text applies).
        """
        inputs_dir = (record.project_dir / ".agentic" / "inputs") if record.project_dir else self.inputs_dir
        submit_file = inputs_dir / f"{form_id}.yaml"
        payload = self._read_submit_payload(submit_file)
        if payload is None or "apply_status" not in payload:
            return None
        if payload.get("apply_status") == "applied":
            return self._stored_apply_result(payload)
        # Apply not completed — re-run it from the saved payload, guarded
        # so two parallel resubmits don't both run it.
        with _reapply_lock(form_id):
            payload = self._read_submit_payload(submit_file)
            if payload is None:
                return None
            if payload.get("apply_status") == "applied":
                return self._stored_apply_result(payload)
            previous = payload.get("apply_result")
            if not isinstance(previous, dict):
                previous = {}
            apply_result = apply_form_submit(
                payload.get("template") or record.template,
                payload.get("data") or {},
                record.project_dir,
            )
            result = {
                **apply_result.to_dict(),
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "attempts": int(previous.get("attempts") or 0) + 1,
            }
            payload["apply_status"] = "applied" if apply_result.ok else "failed"
            payload["apply_result"] = result
            try:
                _atomic_write_yaml(submit_file, payload)
            except OSError as e:
                # The apply ran; the file keeps the old state. The next
                # resubmit re-runs the idempotent apply and records it.
                log.error("Failed to record reapply result for %s: %s", submit_file, e)
        return result

    @staticmethod
    def _stored_apply_result(payload: dict[str, Any]) -> dict[str, Any]:
        result = payload.get("apply_result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _read_submit_payload(submit_file: Path) -> dict[str, Any] | None:
        try:
            import yaml

            data = yaml.safe_load(submit_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        return data if isinstance(data, dict) else None

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

    def _send_text(self, code: int, body: str, close: bool = False) -> None:
        body_bytes = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            if close:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body_bytes)
        except OSError:
            # A-07: мёртвый сокет клиента не превращает отказ в traceback
            # (в MCP-контексте шум stderr — это порча stdio-протокола).
            pass
        if close:
            self.close_connection = True

    def _send_json(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A-07: ThreadingHTTPServer с ограничением активных обработчиков.

    Без предела наводка из «зависших» соединений (open и тишина) порождает
    потоки без конца — по одному на принятое соединение. Перелив получает
    немедленный 503 и закрытие соединения; запрос при этом не
    обрабатывается.
    """

    max_concurrent = MAX_CONCURRENT_HANDLERS

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._active = 0
        self._active_lock = threading.Lock()

    def process_request_thread(self, request, client_address):
        with self._active_lock:
            self._active += 1
            over = self._active > self.max_concurrent
        try:
            if over:
                self._reject_overflow(request)
                return
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1

    @staticmethod
    def _reject_overflow(request: socket.socket) -> None:
        text = b"Too many concurrent form requests"
        response = (
            b"HTTP/1.0 503 Service Unavailable\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(text)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + text
        )
        try:
            request.sendall(response)
        except OSError:
            pass
        request.close()


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
    # A-07: bounded — число активных обработчиков ограничено.
    server = _BoundedThreadingHTTPServer(("127.0.0.1", port), handler_class)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="awf-ui-http")
    thread.start()
    log.info("HTTP server listening on http://127.0.0.1:%d", port)
    return server, port



def apply_form_submit(template: str, data: dict[str, Any], project_dir: Path | None) -> ApplyResult:
    """A-05: run the submit's materialization and report the outcome.

    project-setup → role save/delete + apply_project_setup (roles_processor);
    increment-planning → apply_increment_plan; other templates have nothing
    to materialize (empty ok result).
    """
    if template == "project-setup":
        return _apply_project_setup_submit(data, project_dir)
    if template == "increment-planning":
        return _apply_increment_planning_submit(data, project_dir)
    return ApplyResult(ok=True)


def _apply_project_setup_submit(data: dict[str, Any], project_dir: Path | None) -> ApplyResult:
    from .roles_processor import RoleOpReport, process_role_deletions, process_role_saves

    report = RoleOpReport()
    saved = process_role_saves(data, project_dir=project_dir, report=report)
    deleted = process_role_deletions(data, project_dir=project_dir, report=report)
    if saved:
        report.applied.append(f"roles saved: {saved}")
    if deleted:
        report.applied.append(f"roles deleted: {deleted}")
    return ApplyResult(
        ok=not report.errors,
        applied=report.applied,
        errors=report.errors,
        warnings=report.warnings,
    )


def _apply_increment_planning_submit(data: dict[str, Any], project_dir: Path | None) -> ApplyResult:
    if not project_dir:
        return ApplyResult(ok=True, warnings=["no project_dir — plan.md not materialized"])
    selected = str(data.get("selected_variant", "") or "").strip()
    if not selected or selected == "__reject__":
        return ApplyResult(ok=True, warnings=["no variant selected — plan.md unchanged"])
    variants_json = data.get("variants_json", "") or "[]"
    try:
        variants = json.loads(variants_json) if isinstance(variants_json, str) else variants_json
        if not isinstance(variants, list):
            variants = []
    except (json.JSONDecodeError, TypeError) as e:
        return ApplyResult(ok=False, errors=[f"variants_json is not valid JSON: {e}"])
    selected_variant = next(
        (v for v in variants if isinstance(v, dict) and v.get("id") == selected),
        None,
    )
    if not selected_variant:
        return ApplyResult(ok=True, warnings=[f"variant '{selected}' not found — plan.md unchanged"])
    plan_body = _build_plan_md_from_variant(selected_variant, variants)
    try:
        from awf.api import apply_increment_plan

        apply_increment_plan(
            project_dir,
            plan_body,
            selected_variant_id=selected,
            variants=variants,
        )
        log.info("Increment plan persisted: variant=%s → plan.md", selected)
        return ApplyResult(ok=True, applied=[f"plan.md (variant {selected})"])
    except Exception as e:
        log.error("apply_increment_plan failed: %s", e)
        return ApplyResult(ok=False, errors=[f"apply_increment_plan: {e}"])


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
