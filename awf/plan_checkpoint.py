"""BD-36: Plan checkpoint — preview TODO before agents start.

After supervisor's plan stage, opens an HTML form in the user's browser
with the TODO content and three buttons:

  - **Approve**: continue pipeline (agents start).
  - **Edit**: replace TODO content with user's edits, continue.
  - **Reject**: stop pipeline, supervisor replans on next ``awf start``.

Bypass:

  - ``--auto`` mode (CI/tests): checkpoint skipped automatically.
  - ``automation.plan_checkpoint: false`` in ``config.yaml``.
  - ``AWF_PLAN_CHECKPOINT=false`` env var for one-shot override.

Architecture: awf-core runs a one-shot HTTP server on a random port to
receive the form POST. Self-contained — does not depend on the
agent-workflow-ui plugin being loaded.
"""
from __future__ import annotations

import html as html_lib
import os
import socket
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import config as cfg_mod
from ._atomic import atomic_write_text
from ._log import log as _log

DEFAULT_CHECKPOINT_TIMEOUT = 3600  # 1 hour — matches AWF_SUPERVISOR_TIMEOUT


# ── Public API ───────────────────────────────────────────────────────────────


def is_checkpoint_enabled(config: dict | None, auto: bool) -> bool:
    """BD-36: should the checkpoint run for this pipeline?

    Disabled by (any one is enough):
      - ``auto=True`` (CI/tests via ``--auto``)
      - env ``AWF_PLAN_CHECKPOINT`` in (false/0/no)
      - config ``automation.plan_checkpoint`` falsy
    """
    if auto:
        return False
    env_val = os.environ.get("AWF_PLAN_CHECKPOINT", "").lower()
    if env_val in ("false", "0", "no"):
        return False
    if config is not None:
        val = cfg_mod.get(config, "automation.plan_checkpoint", "true")
        if isinstance(val, str):
            val = val.lower()
        if val in (False, "false", "0", "no", "off"):
            return False
    return True


def run_plan_checkpoint(
    todo_id: str,
    project_dir: Path,
    config: dict | None,
    logs_dir: Path,
    timeout: int = DEFAULT_CHECKPOINT_TIMEOUT,
) -> str:
    """Open HTML form with TODO preview, wait for user decision.

    Returns one of: ``"approve"``, ``"edit"``, ``"reject"``, ``"timeout"``.

    Side effects:
      - On ``"edit"``: rewrites ``.agentic/inbox/{todo_id}.md`` with user edits.
      - On ``"timeout"``: leaves TODO untouched, lets orchestrator decide.
    """
    todo_md = project_dir / ".agentic" / "inbox" / f"{todo_id}.md"
    if not todo_md.is_file():
        _log(logs_dir, f"BD-36: no {todo_id}.md to preview — auto-approve")
        return "approve"

    todo_content = todo_md.read_text(encoding="utf-8")

    plan_md = project_dir / ".agentic" / "phases" / "plan.md"
    plan_content = plan_md.read_text(encoding="utf-8") if plan_md.is_file() else ""

    port = _find_free_port()
    decision_holder: dict[str, str] = {}
    edited_holder: dict[str, str] = {}

    server = _start_checkpoint_server(
        port=port,
        decision_holder=decision_holder,
        edited_holder=edited_holder,
    )

    html_path = ""
    try:
        html_body = _render_html(todo_id, todo_content, plan_content, port)
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".html",
            prefix=f"awf-checkpoint-{todo_id}-",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(html_body)
            html_path = f.name

        print()
        print("=" * 60)
        print(f"  BD-36: Plan Checkpoint — {todo_id}")
        print("=" * 60)
        print(f"  Form:   file://{html_path}")
        print(f"  Server: http://127.0.0.1:{port}")
        print(f"  Timeout: {timeout}s (auto-approve on expiry)")
        print("=" * 60)
        try:
            webbrowser.open(f"file://{html_path}")
        except Exception as e:
            # BUG-4 fix: log the failure so debugging is possible if the
            # URL print above is missed. Non-fatal — user can open manually.
            _log(logs_dir, f"BD-36: webbrowser.open failed: {e} — open URL manually")

        _log(logs_dir, f"BD-36: checkpoint opened for {todo_id} on port {port}")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if decision_holder:
                break
            time.sleep(1)

        # BUG-1 fix: grace period after loop exit. The POST handler runs in
        # a separate thread — between loop exit and this check, an in-flight
        # POST could still populate decision_holder. 200ms covers typical
        # thread scheduling latency without measurable UX impact.
        if not decision_holder:
            time.sleep(0.2)

        if not decision_holder:
            _log(logs_dir, f"BD-36: checkpoint timeout for {todo_id} — auto-approve")
            return "timeout"

        decision = decision_holder["decision"]
        _log(logs_dir, f"BD-36: checkpoint decision for {todo_id}: {decision}")

        # BUG-3 fix: empty edited_content would silently wipe the TODO.
        # Treat as no-op (approve path) and log the rejection.
        if decision == "edit":
            content = edited_holder.get("content", "")
            if not content.strip():
                _log(
                    logs_dir,
                    f"BD-36: edit ignored — empty content submitted for {todo_id}",
                )
                return "approve"
            # BUG-2 fix: atomic_write_text (temp + rename) instead of write_text
            # (truncate-then-write). Survives crash mid-write.
            atomic_write_text(todo_md, content)
            _log(logs_dir, f"BD-36: {todo_id}.md rewritten via edit")

        return decision
    finally:
        server.shutdown()
        server.server_close()
        if html_path:
            try:
                Path(html_path).unlink()
            except OSError:
                pass


# ── HTTP server ──────────────────────────────────────────────────────────────


def _find_free_port() -> int:
    """Ask OS for a free port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_checkpoint_server(
    port: int,
    decision_holder: dict[str, str],
    edited_holder: dict[str, str],
) -> ThreadingHTTPServer:
    """Start one-shot HTTP server to receive form POST. Daemon thread.

    The handler populates ``decision_holder`` (and ``edited_holder`` on edit)
    and returns a small ack page. Server is shut down by the caller after
    the decision arrives or timeout fires.
    """

    class _CheckpointHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — http.server API
            # Browser may request /favicon.ico after form submit. Without a
            # GET handler, BaseHTTPRequestHandler returns 501 and pollutes
            # the browser console. Return 204 No Content for any GET.
            self.send_response(204)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 — http.server API
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8", errors="replace")
            params = parse_qs(body, keep_blank_values=True)

            decision = params.get("decision", [""])[0]
            if decision not in ("approve", "edit", "reject"):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Invalid decision")
                return

            decision_holder["decision"] = decision
            if decision == "edit":
                edited_holder["content"] = params.get("edited_content", [""])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ack = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>awf</title></head>"
                "<body style='font-family:sans-serif;padding:40px;text-align:center'>"
                f"<h2>Решение: {html_lib.escape(decision)}</h2>"
                "<p>Можно закрыть вкладку. awf продолжит работу.</p>"
                "</body></html>"
            )
            self.wfile.write(ack.encode("utf-8"))

        def log_message(self, *args, **kwargs) -> None:
            pass  # silence stderr noise

    server = ThreadingHTTPServer(("127.0.0.1", port), _CheckpointHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── HTML template ────────────────────────────────────────────────────────────


def _render_html(
    todo_id: str,
    todo_content: str,
    plan_content: str,
    port: int,
) -> str:
    """Render checkpoint HTML form (inline, no Jinja dependency in awf-core)."""
    submit_url = f"http://127.0.0.1:{port}/checkpoint"
    todo_esc = html_lib.escape(todo_content)
    plan_esc = html_lib.escape(plan_content) if plan_content else "(нет plan.md)"
    todo_id_esc = html_lib.escape(todo_id)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>awf Plan Checkpoint — {todo_id_esc}</title>
  <style>
    body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 920px;
           margin: 30px auto; padding: 0 20px; color: #222; }}
    h1 {{ font-size: 22px; border-bottom: 2px solid #4a90d9; padding-bottom: 8px; }}
    h2 {{ font-size: 15px; margin-top: 24px; color: #555;
          text-transform: uppercase; letter-spacing: 0.5px; }}
    .preview {{ background: #f7f7f7; border-left: 4px solid #4a90d9; padding: 12px 16px;
               white-space: pre-wrap; font-family: ui-monospace, monospace;
               font-size: 13px; line-height: 1.5; max-height: 400px; overflow-y: auto;
               border-radius: 0 4px 4px 0; }}
    .plan {{ background: #fafafa; padding: 8px 12px; white-space: pre-wrap;
            font-family: ui-monospace, monospace; font-size: 12px; color: #666;
            max-height: 200px; overflow-y: auto; border-radius: 4px; }}
    textarea {{ width: 100%; min-height: 320px; font-family: ui-monospace, monospace;
               font-size: 13px; padding: 12px; box-sizing: border-box;
               border: 1px solid #ccc; border-radius: 4px; display: none; }}
    .actions {{ margin-top: 24px; display: flex; gap: 12px; flex-wrap: wrap; }}
    button {{ padding: 12px 24px; font-size: 15px; border: none; border-radius: 6px;
             cursor: pointer; font-weight: 600; }}
    .approve {{ background: #28a745; color: white; }}
    .edit {{ background: #ffc107; color: #222; }}
    .reject {{ background: #dc3545; color: white; }}
    .approve:hover {{ background: #218838; }}
    .edit:hover {{ background: #e0a800; }}
    .reject:hover {{ background: #c82333; }}
    .note {{ font-size: 12px; color: #888; margin-top: 16px; line-height: 1.5; }}
  </style>
</head>
<body>
  <h1>Plan Checkpoint — {todo_id_esc}</h1>
  <p>Supervisor создал TODO. Проверь содержимое перед запуском агентов.</p>

  <h2>Контекст — phases/plan.md</h2>
  <div class="plan">{plan_esc}</div>

  <h2>TODO content</h2>
  <div class="preview" id="preview">{todo_esc}</div>
  <textarea id="editor" name="edited_content">{todo_esc}</textarea>

  <div class="actions">
    <button type="button" class="approve" onclick="submitDecision('approve')">✓ Утвердить</button>
    <button type="button" class="edit" id="editBtn" onclick="toggleEdit()">✏ Изменить</button>
    <button type="button" class="reject" onclick="submitDecision('reject')">✗ Отклонить</button>
  </div>

  <div class="note">
    <b>Утвердить</b> → агенты запустятся.<br>
    <b>Изменить</b> → отредактируй текст в поле ниже и подтверди.<br>
    <b>Отклонить</b> → pipeline остановится, supervisor перепланирует при следующем <code>awf start</code>.
  </div>

  <script>
    let editing = false;
    function toggleEdit() {{
      editing = !editing;
      const preview = document.getElementById('preview');
      const editor = document.getElementById('editor');
      const btn = document.getElementById('editBtn');
      if (editing) {{
        preview.style.display = 'none';
        editor.style.display = 'block';
        editor.focus();
        btn.textContent = '✓ Подтвердить правки';
      }} else {{
        submitDecision('edit');
      }}
    }}
    function submitDecision(decision) {{
      const form = document.createElement('form');
      form.method = 'POST';
      form.action = '{submit_url}';
      const d = document.createElement('input');
      d.type = 'hidden'; d.name = 'decision'; d.value = decision;
      form.appendChild(d);
      if (decision === 'edit') {{
        const c = document.createElement('input');
        c.type = 'hidden'; c.name = 'edited_content';
        c.value = document.getElementById('editor').value;
        form.appendChild(c);
      }}
      document.body.appendChild(form);
      form.submit();
    }}
  </script>
</body>
</html>"""
