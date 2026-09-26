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
  - ``no_checkpoints=True`` on ``awf_start`` / ``awf_continue`` (RUN3 #6):
    a single-launch parameter — ``AWF_NO_CHECKPOINTS=1`` is set for the
    duration of the pipeline process and dies with it (not written to
    config or state, the next launch is unaffected).

Architecture: awf-core runs a one-shot HTTP server on a random port to
receive the form POST. Self-contained — does not depend on the
agent-workflow-ui plugin being loaded.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import secrets
import socket
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import config as cfg_mod
from ._atomic import atomic_write_text
from ._log import log as _log

DEFAULT_CHECKPOINT_TIMEOUT = 3600  # 1 hour — matches AWF_SUPERVISOR_TIMEOUT

# A-07 (аудит 2026-09-25, слой 5): пределы тела POST формы. Настоящая форма
# шлёт килобайты (decision + отредактированный TODO + токен), поэтому лимит
# — защита от мусора и потоков, а не функциональное ограничение.
CHECKPOINT_BODY_MAX_BYTES = 64 * 1024  # 64 KiB — потолок тела решения
CHECKPOINT_BODY_READ_TIMEOUT = 10.0  # секунд простоя сокета на чтении
CHECKPOINT_MAX_CONCURRENT = 16  # максимум одновременных обработчиков


def _live_checkpoint_form_file(project_dir: Path) -> Path | None:
    """AUD03-03: form file a live checkpoint is waiting on, if any.

    Returns the path named in state ``checkpoint_form_url`` when
    ``checkpoint_pending`` is true. Missing/corrupt state or a resolved
    checkpoint → None (nothing to protect).
    """
    from .pipeline_state import read_state

    state = read_state(project_dir)
    if not state or not state.get("checkpoint_pending"):
        return None
    form_url = state.get("checkpoint_form_url")
    if not isinstance(form_url, str) or not form_url.startswith("file://"):
        return None
    return Path(form_url[len("file://"):])


def _cleanup_stale_temp_html(project_dir: Path) -> int:
    """П7: remove leftover awf-checkpoint-*.html from crashed runs.

    Without this, every crash leaves a temp HTML file. On next awf start,
    webbrowser.open may fire for the new file, but stale ones from
    previous runs are still in temp dir — confusing if user opens them
    manually (they show old TODO content / outdated submit URL).

    Safety:
    - Only removes files older than 10 minutes — protects a temp HTML that
      a concurrently-running awf instance just created.
    - AUD03-03: NEVER removes the file a live checkpoint state references
      (``checkpoint_pending=True`` + ``checkpoint_form_url``). The 10-minute
      threshold is shorter than DEFAULT_CHECKPOINT_TIMEOUT (60 min), so the
      age rule alone would wipe an idle user's form mid-run.

    Returns count of files removed. Best-effort: ignores permission errors.
    """
    import glob
    import tempfile
    tmp_dir = tempfile.gettempdir()
    pattern = f"{tmp_dir}/awf-checkpoint-*.html"

    protected = _live_checkpoint_form_file(project_dir)

    # Note: st_mtime is wall-clock (epoch). Must compare with time.time(),
    # NOT time.monotonic() (which has arbitrary zero point).
    now = time.time()
    removed = 0
    for stale in glob.glob(pattern):
        try:
            # AUD03-03: live form survives any timeout.
            if protected is not None and Path(stale) == protected:
                continue
            # Only remove files older than 10 minutes — protects a
            # concurrently-running awf instance that just created its form.
            mtime = Path(stale).stat().st_mtime
            if (now - mtime) < 600:
                continue
            Path(stale).unlink()
            removed += 1
        except OSError:
            pass  # permission/locked — skip silently
    return removed


# ── Public API ───────────────────────────────────────────────────────────────


def is_checkpoint_enabled(
    config: dict | None,
    auto: bool,
    *,
    no_checkpoints: bool = False,
) -> bool:
    """BD-36: should the checkpoint run for this pipeline?

    Disabled by (any one is enough):
      - ``no_checkpoints=True`` (B4: run flag — an autonomous run that does
        not expect checkpoints; passed by the engine from the active run)
      - ``auto=True`` (CI/tests via ``--auto``)
      - env ``AWF_PLAN_CHECKPOINT`` in (false/0/no)
      - config ``automation.plan_checkpoint`` falsy
    """
    if no_checkpoints:
        return False
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


LAUNCH_NO_CHECKPOINTS_ENV = "AWF_NO_CHECKPOINTS"


def launch_no_checkpoints() -> bool:
    """RUN3 #6: the single-launch ``no_checkpoints`` parameter, as env.

    ``start_pipeline`` / ``continue_pipeline`` set ``AWF_NO_CHECKPOINTS=1``
    for the duration of the pipeline process when called with
    ``no_checkpoints=True`` (background: on the child at spawn, next to
    ``AWF_BACKGROUND_CHILD``; foreground: around ``run_pipeline``, restored
    on every exit path — same pattern as ``AWF_SUPERVISOR_TIMEOUT``). The
    engine gate reads it here, so the flag cannot survive the launch: no
    config, no state file, the next launch is unaffected.

    Truthy values: 1 / true / yes (case-insensitive) — the same set the
    ``AWF_PLAN_CHECKPOINT`` override understands in its false form.
    """
    return os.environ.get(LAUNCH_NO_CHECKPOINTS_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


# ── B1: decision survives pipeline death ─────────────────────────────────────


def _checkpoint_decision_file(project_dir: Path, todo_id: str) -> Path:
    """B1: where a submitted checkpoint decision is persisted so it survives
    the pipeline process dying. Applied once, then renamed with a
    ``.consumed`` suffix (audit trail)."""
    return project_dir / ".agentic" / "context" / f"CHECKPOINT-{todo_id}.json"


def _persist_checkpoint_decision(
    decision_file: Path,
    todo_id: str,
    decision: str,
    content: str,
    logs_dir: Path | None,
) -> None:
    """B1: atomically save the form decision to disk (temp + rename).

    Best-effort: a write failure degrades to the in-memory path only — the
    live pipeline still applies the decision, only the survival guarantee
    is lost.
    """
    payload = {
        "decision": decision,
        "todo_id": todo_id,
        "content": content,
        "submitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            decision_file, json.dumps(payload, ensure_ascii=False, indent=2)
        )
    except OSError:
        if logs_dir is not None:
            _log(
                logs_dir,
                f"B1: failed to persist checkpoint decision to {decision_file}",
            )


def _consume_checkpoint_decision(
    project_dir: Path, todo_id: str, logs_dir: Path | None
) -> bool:
    """B1: mark a saved decision as consumed.

    ``os.replace`` is atomic on POSIX — race-free by construction: of two
    concurrent gate entries only one wins the rename; the loser sees no file
    and opens a fresh form. Returns True when a decision file was consumed.
    """
    f = _checkpoint_decision_file(project_dir, todo_id)
    if not f.is_file():
        return False
    consumed = f.parent / (f.name + ".consumed")
    try:
        os.replace(f, consumed)
    except OSError:
        return False
    if logs_dir is not None:
        _log(logs_dir, f"B1: checkpoint decision consumed: {f.name} -> {consumed.name}")
    return True


def _apply_pending_checkpoint_decision(
    todo_id: str, content_file: Path, project_dir: Path, logs_dir: Path
) -> str | None:
    """B1: apply an unconsumed saved decision without opening the form.

    A previous checkpoint run may have died after the user's submit (timeout
    teardown race, crash, manual stop) — the decision survived on disk.
    Applying it here is the pending-signal path: the next pipeline entry
    continues instead of re-asking a question the user already answered.

    Returns the decision ("approve"/"edit"/"reject") or None (no valid saved
    decision — the form opens as before).
    """
    f = _checkpoint_decision_file(project_dir, todo_id)
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        _log(
            logs_dir,
            f"B1: {f.name} is unreadable or corrupt — ignoring, opening a fresh form",
        )
        return None
    if not isinstance(data, dict):
        _log(
            logs_dir,
            f"B1: {f.name} is not a JSON object — ignoring, opening a fresh form",
        )
        return None
    decision = str(data.get("decision", ""))
    if decision not in ("approve", "edit", "reject"):
        _log(
            logs_dir,
            f"B1: {f.name} has invalid decision {decision!r} "
            f"— ignoring, opening a fresh form",
        )
        return None
    content = str(data.get("content", ""))
    if decision == "edit":
        if not content.strip():
            # BUG-3 parity: an empty edit must never wipe the TODO.
            _log(
                logs_dir,
                f"B1: saved edit for {todo_id} has empty content — treated as approve",
            )
            decision = "approve"
        else:
            atomic_write_text(content_file, content)
    _consume_checkpoint_decision(project_dir, todo_id, logs_dir)
    _log(
        logs_dir,
        f"B1: applying saved checkpoint decision for {todo_id}: {decision} (no form)",
    )
    return decision


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
    # FU-05: the TODO is the only contract — the checkpoint previews it
    todo_md = project_dir / ".agentic" / "inbox" / f"{todo_id}.md"
    if todo_md.is_file():
        content_file = todo_md
        content_label = "TODO"
    else:
        _log(logs_dir, f"BD-36: no {todo_id}.md to preview — auto-approve")
        return "approve"

    # B1: a decision saved by a previous (died) checkpoint run is applied
    # without re-opening the form — the user already answered this question.
    # Runs BEFORE the P1 hash-skip so a pending edit is never shadowed by a
    # matching pre-edit hash.
    pending = _apply_pending_checkpoint_decision(
        todo_id, content_file, project_dir, logs_dir
    )
    if pending is not None:
        return pending

    # AUD03-07: preview content — a corrupted (non-UTF-8) file must degrade,
    # not crash the pipeline main-loop. errors="replace" keeps the preview
    # renderable (the user sees replacement chars instead of a traceback).
    todo_content = content_file.read_text(encoding="utf-8", errors="replace")

    # P1: Skip checkpoint if content unchanged since last approval (kill+start loop)
    import hashlib
    content_hash = hashlib.sha256(todo_content.encode("utf-8")).hexdigest()[:16]
    hash_file = project_dir / ".agentic" / "context" / "checkpoint-approved.hash"
    if hash_file.is_file():
        # AUD03-04: stored entry is "{todo_id}:{hash}" — the same text under
        # a different TODO id is a new plan and must not be silently approved.
        last_todo, _, last_hash = hash_file.read_text(encoding="utf-8").strip().partition(":")
        if last_todo == todo_id and last_hash == content_hash:
            _log(logs_dir, f"P1: checkpoint skipped — content unchanged (hash={content_hash})")
            print("BD-36: Plan checkpoint skipped — same as last approved plan.")
            return "approve"

    # П7: clean up stale temp HTML from previous (crashed) runs before
    # creating our own. Without this, user may see old forms from /tmp/.
    # AUD03-03: a form referenced by a live checkpoint state is protected.
    stale_count = _cleanup_stale_temp_html(project_dir)
    if stale_count:
        _log(logs_dir, f"П7: removed {stale_count} stale checkpoint HTML file(s)")

    plan_md = project_dir / ".agentic" / "phases" / "plan.md"
    # AUD03-07: same as above — plan.md is preview content, degrade on garbage.
    plan_content = (
        plan_md.read_text(encoding="utf-8", errors="replace") if plan_md.is_file() else ""
    )

    port = 0  # P2: let OS assign free port — eliminates TOCTOU race entirely
    # A-03: one-time token binds the decision to this opened form — a local
    # client that only learned the port cannot submit a decision without it.
    token = secrets.token_urlsafe(32)
    decision_holder: dict[str, str] = {}
    edited_holder: dict[str, str] = {}
    # AUD03-05: first-wins — the check-and-set in do_POST happens under this
    # lock so two racing POSTs cannot both "win".
    decision_lock = threading.Lock()

    # DAUD-2: port=0 → OS picks free port at bind() time, no race window.
    server = None
    for attempt in range(3):
        try:
            server = _start_checkpoint_server(
                port=port,
                decision_holder=decision_holder,
                edited_holder=edited_holder,
                decision_lock=decision_lock,
                decision_file=_checkpoint_decision_file(project_dir, todo_id),
                todo_id=todo_id,
                logs_dir=logs_dir,
                token=token,
            )
            break
        except OSError:
            if attempt < 2:
                import time as _time
                _time.sleep(0.5)
            else:
                raise

    html_path = ""
    actual_port = server.server_address[1]  # P2: OS-assigned port (was port=0)
    try:
        html_body = _render_html(
            todo_id, todo_content, plan_content, actual_port, token
        )
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
        print(f"  Server: http://127.0.0.1:{actual_port}")
        print(f"  Timeout: {timeout}s (pipeline aborts on expiry — re-run awf_start to retry)")
        print("=" * 60)
        try:
            webbrowser.open(f"file://{html_path}")
        except Exception as e:
            # BUG-4 fix: log the failure so debugging is possible if the
            # URL print above is missed. Non-fatal — user can open manually.
            _log(logs_dir, f"BD-36: webbrowser.open failed: {e} — open URL manually")

        # AUD03-02: port=0 is only the bind request — every surface (logs,
        # state) must carry actual_port, the OS-assigned one.
        _log(logs_dir, f"BD-36: checkpoint opened for {todo_id} on port {actual_port}")
        # Dogfood-8: also log form URL so api.get_status can extract it
        # and surface to supervisor (who tells user where to approve).
        _log(logs_dir, f"BD-36: form_url=file://{html_path}")
        _log(logs_dir, f"BD-36: server_url=http://127.0.0.1:{actual_port}")
        # T4.1: persist checkpoint state to structured file (no regex needed)
        from .pipeline_state import write_state
        write_state(
            project_dir,
            logs_dir=logs_dir,
            checkpoint_pending=True,
            checkpoint_port=actual_port,
            checkpoint_form_url=f"file://{html_path}",
        )

        # B2: the form wait is downtime — the pipeline is alive but no work
        # happens until the owner decides. Measured from the first check to
        # the decision (or timeout). The form-decision wait IS the
        # checkpoint wait, so it is recorded exactly once, here.
        wait_start = time.monotonic()
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

        from . import run_state as _run_state
        _run_state.add_downtime(
            project_dir, time.monotonic() - wait_start,
            reason=f"checkpoint-wait:{todo_id}", logs_dir=logs_dir,
        )

        if not decision_holder:
            # QA: orchestrator treats "timeout" as ABORT (returns 1), not
            # auto-approve. Log message must reflect that — was misleading.
            # B1: a submit that lands while the form is alive is persisted;
            # the owner then continues with `awf continue` and the decision
            # is applied on the next gate entry (policy unchanged: timeout
            # is still NOT an auto-approve).
            dec_file = _checkpoint_decision_file(project_dir, todo_id)
            _log(
                logs_dir,
                f"BD-36: checkpoint timeout for {todo_id} — pipeline will abort. "
                f"Форма сохранена: {dec_file}; после сабмита — `awf continue`, "
                f"решение подхватится без повторной формы.",
            )
            # AUD02-03: the form is dead now (the server shuts down in
            # finally) — clear the pending keys in the moment, so a later
            # awf_status/awf_wait_for_event between the abort and the next
            # run does not point at a form that can never be approved.
            from .pipeline_state import write_state
            write_state(
                project_dir,
                logs_dir=logs_dir,
                checkpoint_pending=False,
                checkpoint_port=None,
                checkpoint_form_url=None,
            )
            return "timeout"

        decision = decision_holder["decision"]
        _log(logs_dir, f"BD-36: checkpoint decision for {todo_id}: {decision}")
        # B1: the submit also persisted a decision file — consume it now so
        # a later gate entry (kill+start loop) does not apply the same
        # decision twice.
        _consume_checkpoint_decision(project_dir, todo_id, logs_dir)
        # T4.1: persist checkpoint resolution (no longer pending).
        # AUD02-11: checkpoint_decision was written but never read — removed
        # (a dead key is a false contract signal; the decision is also in the
        # orchestrator.log line above).
        from .pipeline_state import write_state
        write_state(
            project_dir,
            logs_dir=logs_dir,
            checkpoint_pending=False,
        )

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
            atomic_write_text(content_file, content)
            _log(logs_dir, f"BD-36: {content_label} {content_file.name} rewritten via edit")

        # P1: Save content hash for skip-on-unchanged
        if decision in ("approve", "edit"):
            if decision == "edit":
                # AUD03-04: hash what was ACTUALLY written, not the pre-edit
                # content — otherwise kill+start right after an edit re-shows
                # the form for the just-approved text.
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            try:
                hash_file.parent.mkdir(parents=True, exist_ok=True)
                hash_file.write_text(f"{todo_id}:{content_hash}\n", encoding="utf-8")
            except OSError:
                pass

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


def _is_local_origin(origin: str) -> bool:
    """QA-A/KAUD-3: CSRF defense — check if Origin/Referer is localhost.

    Uses urlparse hostname check (not startswith, which allowed bypass
    via http://127.0.0.1.evil.com).
    """
    from urllib.parse import urlparse

    ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

    if not origin:
        return True  # no Origin header — backward compat
    if origin == "null":
        return True  # sandboxed/local file origin
    try:
        parsed = urlparse(origin)
        return parsed.hostname in ALLOWED_HOSTS
    except (ValueError, TypeError):
        return False


class _BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """A-07: ThreadingHTTPServer с ограничением активных обработчиков.

    Без предела наводка из «зависших» соединений (open и тишина) порождает
    потоки без конца — по одному на принятое соединение. Перелив получает
    немедленный 503 и закрытие соединения; запрос при этом не
    обрабатывается.
    """

    max_concurrent = CHECKPOINT_MAX_CONCURRENT

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
        text = b"Too many concurrent checkpoint requests"
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


def _start_checkpoint_server(
    port: int,
    decision_holder: dict[str, str],
    edited_holder: dict[str, str],
    decision_lock: threading.Lock | None = None,
    decision_file: Path | None = None,
    todo_id: str | None = None,
    logs_dir: Path | None = None,
    token: str | None = None,
) -> ThreadingHTTPServer:
    """Start one-shot HTTP server to receive form POST. Daemon thread.

    The handler populates ``decision_holder`` (and ``edited_holder`` on edit)
    and returns a small ack page. Server is shut down by the caller after
    the decision arrives or timeout fires.

    AUD03-05: decision writing is first-wins, guarded by ``decision_lock``
    (shared across all handler instances of this server).

    B1: when ``decision_file`` + ``todo_id`` are given, the first accepted
    POST also persists the decision to disk (before publishing it in-memory)
    so it survives the process dying.

    A-03: ``token`` is the opened form's one-time token. A POST is accepted
    only on path ``/checkpoint`` and only with this token; the check runs
    under ``decision_lock`` and the first accepted POST invalidates the
    token (default-deny: without a token the server accepts nothing).
    """
    if decision_lock is None:
        decision_lock = threading.Lock()
    # A-03: the form's one-time token (None — no POST can be accepted).
    token_state: dict[str, str | None] = {"token": token}

    class _CheckpointHandler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            # A-07: любое чтение на этом соединении (request line, тело)
            # ограничено по времени — зависший клиент не удерживает поток
            # обработчика. socket.timeout на request line уже обработан
            # BaseHTTPRequestHandler (закрытие без traceback).
            self.connection.settimeout(CHECKPOINT_BODY_READ_TIMEOUT)

        def _reply(self, code: int, text: bytes, *, close: bool = False) -> None:
            """A-07: маленький текстовый отказ (400/408/413). Мёртвый сокет
            клиента не превращает сам отказ в traceback."""
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(text)))
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(text)
                if close:
                    self.close_connection = True
            except OSError:
                pass

        def do_GET(self) -> None:  # noqa: N802 — http.server API
            # Browser may request /favicon.ico after form submit. Without a
            # GET handler, BaseHTTPRequestHandler returns 501 and pollutes
            # the browser console. Return 204 No Content for any GET.
            self.send_response(204)
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 — http.server API
            # A-07: Content-Length валидируется до любого чтения: мусор или
            # отрицательное число — немедленный 400, а не int()/read(-1) в
            # потоке (traceback или ожидание конца потока).
            length_raw = self.headers.get("Content-Length")
            try:
                length = int(length_raw) if length_raw is not None else 0
            except ValueError:
                self._reply(400, b"Bad Content-Length")
                return
            if length < 0 or length > CHECKPOINT_BODY_MAX_BYTES:
                # A-07: перелив — 413 без вычитывания тела: соединение
                # закрывается, поток освобождается, заявленные мегабайты
                # не читаются в память. Отрицательное — 400.
                if length < 0:
                    self._reply(400, b"Bad Content-Length")
                else:
                    self._reply(
                        413,
                        b"Payload too large: checkpoint limit is 64 KiB",
                        close=True,
                    )
                return

            # QA-A: CSRF check — accept only from localhost forms.
            # Same-origin check via Origin/Referer header. Allows file://
            # forms (Origin: null or absent). Rejects cross-origin POST.
            origin = self.headers.get("Origin", "") or self.headers.get("Referer", "")
            if origin and origin not in ("null",) and not _is_local_origin(origin):
                self.send_response(403)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Forbidden: cross-origin POST rejected")
                return

            # A-07: чтение ограничено по времени (socket timeout выставляется
            # в setup) — медленный клиент не держит поток.
            try:
                body = self.rfile.read(length).decode("utf-8", errors="replace")
            except OSError:  # TimeoutError ⊂ OSError (py3.10+) — read timeout
                self._reply(408, b"Timeout reading body", close=True)
                return
            finally:
                # Таймаут был только для чтения — маленький ответ
                # ограничивать не нужно.
                try:
                    self.connection.settimeout(None)
                except OSError:
                    pass

            params = parse_qs(body, keep_blank_values=True)

            decision = params.get("decision", [""])[0]
            if decision not in ("approve", "edit", "reject"):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Invalid decision")
                return

            # A-03: path + token checks under the same lock that sets the
            # decision. Only the opened form carries the one-time token — a
            # client that learned the port but has no token (or a foreign
            # one) cannot set the decision. The first accepted POST
            # invalidates the token, so first-wins holds among valid-token
            # requests only; repeat/racing POSTs are refused, never
            # overwrite (last-write-wins used to let a second tab flip the
            # outcome — AUD03-05).
            submitted_token = params.get("token", [""])[0]
            rejected: tuple[int, bytes] | None = None
            already_decided = False
            with decision_lock:
                if self.path.split("?", 1)[0] != "/checkpoint":
                    rejected = (
                        404,
                        b"Not Found: POSTs are only accepted on /checkpoint",
                    )
                else:
                    current_token = token_state["token"]
                    # A-03 F1: compare_digest бросает TypeError на не-ASCII
                    # str — isascii() превращает такой токен в чистый 403,
                    # а не сброс соединения + traceback в stderr.
                    if not (
                        current_token is not None
                        and submitted_token
                        and submitted_token.isascii()
                        and secrets.compare_digest(submitted_token, current_token)
                    ):
                        rejected = (
                            403,
                            b"Forbidden: missing or invalid checkpoint token",
                        )
                    elif decision_holder:
                        already_decided = True
                    else:
                        edited_content = params.get("edited_content", [""])[0]
                        # B1: persist to disk BEFORE publishing in-memory —
                        # the main loop wakes on the holder and consumes the
                        # file, so the write must be visible first.
                        if decision_file is not None and todo_id is not None:
                            _persist_checkpoint_decision(
                                decision_file,
                                todo_id,
                                decision,
                                edited_content if decision == "edit" else "",
                                logs_dir,
                            )
                        decision_holder["decision"] = decision
                        if decision == "edit":
                            edited_holder["content"] = edited_content
                        # A-03: one-time — the form's token is spent.
                        token_state["token"] = None

            if rejected is not None:
                self.send_response(rejected[0])
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(rejected[1])
                return

            if already_decided:
                first = decision_holder["decision"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                dup_ack = (
                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                    "<title>awf</title>"
                    "<style>"
                    "body{background:#1e1e1e;color:#d4d4d4;font-family:system-ui,sans-serif;"
                    "padding:40px;text-align:center;margin:0;}"
                    "h2{color:#4ec9b0;font-weight:600;margin-bottom:12px;}"
                    "p{color:#858585;}"
                    "</style>"
                    "</head>"
                    "<body>"
                    f"<h2>Решение уже принято: {html_lib.escape(first)}</h2>"
                    "<p>Повторная отправка проигнорирована. Можно закрыть вкладку.</p>"
                    "</body></html>"
                )
                self.wfile.write(dup_ack.encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ack = (
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<title>awf</title>"
                "<style>"
                "body{background:#1e1e1e;color:#d4d4d4;font-family:system-ui,sans-serif;"
                "padding:40px;text-align:center;margin:0;}"
                "h2{color:#4ec9b0;font-weight:600;margin-bottom:12px;}"
                "p{color:#858585;}"
                "</style>"
                "</head>"
                "<body>"
                f"<h2>Решение: {html_lib.escape(decision)}</h2>"
                "<p>Можно закрыть вкладку. awf продолжит работу.</p>"
                "</body></html>"
            )
            self.wfile.write(ack.encode("utf-8"))

        def log_message(self, *args, **kwargs) -> None:
            pass  # silence stderr noise

    server = _BoundedThreadingHTTPServer(("127.0.0.1", port), _CheckpointHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# ── HTML template ────────────────────────────────────────────────────────────


def _render_html(
    todo_id: str,
    todo_content: str,
    plan_content: str,
    port: int,
    token: str = "",
) -> str:
    """Render checkpoint HTML form (inline, no Jinja dependency in awf-core).

    ``token`` is the form's one-time submit token (A-03): it lives only in
    this rendered file, never in logs or state.
    """
    submit_url = f"http://127.0.0.1:{port}/checkpoint"
    todo_esc = html_lib.escape(todo_content)
    plan_esc = html_lib.escape(plan_content) if plan_content else "(нет plan.md)"
    todo_id_esc = html_lib.escape(todo_id)
    token_esc = html_lib.escape(token)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>awf Plan Checkpoint — {todo_id_esc}</title>
  <style>
    /* Палитра синхронизирована с project-setup.html.j2 (plugin) — единый
       тёмный стиль для всех форм awf. */
    :root {{
      --bg: #1e1e1e;
      --bg-card: #252526;
      --bg-input: #3c3c3c;
      --bg-hover: #094771;
      --text: #d4d4d4;
      --text-muted: #858585;
      --accent: #569cd6;
      --accent-green: #4ec9b0;
      --border: #464647;
      --danger: #f14c4c;
      --warn: #dcdcaa;
      --radius: 6px;
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, sans-serif;
      font-size: 14px;
      max-width: 920px;
      margin: 30px auto;
      padding: 0 20px;
      line-height: 1.5;
    }}

    h1 {{
      font-size: 22px;
      font-weight: 600;
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      margin-bottom: 16px;
    }}

    h2 {{
      font-size: 13px;
      font-weight: 600;
      margin-top: 24px;
      margin-bottom: 6px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }}

    .intro {{ color: var(--text-muted); margin-bottom: 8px; }}

    .preview {{
      background: var(--bg-card);
      border-left: 3px solid var(--accent);
      padding: 12px 16px;
      white-space: pre-wrap;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 13px;
      line-height: 1.5;
      max-height: 400px;
      overflow-y: auto;
      border-radius: 0 var(--radius) var(--radius) 0;
      color: var(--text);
    }}

    .plan {{
      background: var(--bg-card);
      padding: 10px 14px;
      white-space: pre-wrap;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 12px;
      color: var(--text-muted);
      max-height: 180px;
      overflow-y: auto;
      border-radius: var(--radius);
      border: 1px solid var(--border);
    }}

    textarea {{
      width: 100%;
      min-height: 320px;
      font-family: ui-monospace, "SF Mono", Consolas, monospace;
      font-size: 13px;
      padding: 12px;
      box-sizing: border-box;
      background: var(--bg-input);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      display: none;
      line-height: 1.5;
    }}
    textarea:focus {{ outline: none; border-color: var(--accent); }}

    .actions {{
      margin-top: 28px;
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}

    button {{
      padding: 10px 22px;
      font-size: 14px;
      border: none;
      border-radius: var(--radius);
      cursor: pointer;
      font-weight: 600;
      font-family: inherit;
      transition: filter 0.15s, opacity 0.15s;
    }}
    .approve {{ background: var(--accent-green); color: var(--bg); }}
    .edit    {{ background: var(--warn);         color: var(--bg); }}
    .reject  {{ background: var(--danger);       color: white; }}

    button:hover {{ filter: brightness(1.15); }}
    button:active {{ filter: brightness(0.9); }}

    .note {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 20px;
      line-height: 1.7;
      padding: 12px 14px;
      background: var(--bg-card);
      border-radius: var(--radius);
      border: 1px solid var(--border);
    }}
    .note code {{
      background: var(--bg-input);
      padding: 1px 5px;
      border-radius: 3px;
      font-size: 11px;
      color: var(--accent);
    }}
  </style>
</head>
<body>
  <h1>Plan Checkpoint — {todo_id_esc}</h1>
  <p class="intro">Supervisor создал TODO. Проверь содержимое перед запуском агентов.</p>

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
    const formToken = "{token_esc}";
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
      const t = document.createElement('input');
      t.type = 'hidden'; t.name = 'token'; t.value = formToken;
      form.appendChild(t);
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
