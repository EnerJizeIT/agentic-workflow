"""BD-36: Plan checkpoint integration tests.

Coverage matrix:
- TestCheckpointEnabled — bypass logic (auto / env / config)
- TestRenderHtml — template structure, escaping, fields
- TestCheckpointServer — HTTP server POST handling
- TestRunPlanCheckpoint — end-to-end flow (no browser; uses HTTP POST)
"""
from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from conftest import _free_port  # AUD12-09: shared free-port helper

from awf import plan_checkpoint

# A-03: one-time form token. Direct-server tests pass it to the server and
# POST it; e2e tests extract the rendered one from the temp form HTML.
FORM_TOKEN = "integration-form-token"

_FORM_TOKEN_RE = re.compile(r'formToken = "([^"]+)"')


def _extract_form_token(todo_id: str = "TODO-0001") -> str:
    """A-03: the one-time token rendered into the temp form HTML."""
    html_files = sorted(
        Path(tempfile.gettempdir()).glob(f"awf-checkpoint-{todo_id}-*.html")
    )
    assert html_files, "form HTML not found in tempdir"
    html = html_files[-1].read_text(encoding="utf-8")
    m = _FORM_TOKEN_RE.search(html)
    assert m, "formToken not found in form HTML"
    return m.group(1)


# Bound at import, before any monkeypatch: wrappers that call _run_with_post
# twice in one test must each wrap the TRUE original (re-reading the module
# attribute would nest the wrappers and spawn a stray POST thread from the
# earlier call).
_ORIGINAL_START_SERVER = plan_checkpoint._start_checkpoint_server

# ── TestCheckpointEnabled ────────────────────────────────────────────────────


class TestCheckpointEnabled:
    """is_checkpoint_enabled: bypass logic across auto/env/config."""

    def test_default_enabled(self):
        """No config, no auto, no env → enabled."""
        import os
        old = os.environ.pop("AWF_PLAN_CHECKPOINT", None)
        try:
            assert plan_checkpoint.is_checkpoint_enabled(config=None, auto=False) is True
        finally:
            if old is not None:
                os.environ["AWF_PLAN_CHECKPOINT"] = old

    def test_disabled_by_auto(self):
        """auto=True always disables checkpoint (CI/tests)."""
        assert plan_checkpoint.is_checkpoint_enabled(config=None, auto=True) is False

    @pytest.mark.parametrize("env_val", ["false", "0", "no", "FALSE", "No"])
    def test_disabled_by_env(self, monkeypatch, env_val):
        """AWF_PLAN_CHECKPOINT=false/0/no disables for one run."""
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", env_val)
        assert plan_checkpoint.is_checkpoint_enabled(config=None, auto=False) is False

    def test_env_true_does_not_disable(self, monkeypatch):
        """AWF_PLAN_CHECKPOINT=true keeps checkpoint enabled."""
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "true")
        assert plan_checkpoint.is_checkpoint_enabled(config=None, auto=False) is True

    @pytest.mark.parametrize(
        "val", [False, "false", "0", "no", "off", "False", "OFF"],
    )
    def test_disabled_by_config(self, monkeypatch, val):
        """automation.plan_checkpoint: <falsy> in config.yaml disables."""
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        config = {"automation": {"plan_checkpoint": val}}
        assert plan_checkpoint.is_checkpoint_enabled(config=config, auto=False) is False

    def test_config_true_keeps_enabled(self, monkeypatch):
        """automation.plan_checkpoint: true keeps it on."""
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        config = {"automation": {"plan_checkpoint": "true"}}
        assert plan_checkpoint.is_checkpoint_enabled(config=config, auto=False) is True

    def test_config_missing_defaults_to_enabled(self, monkeypatch):
        """Empty config → checkpoint on (default behavior)."""
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        assert plan_checkpoint.is_checkpoint_enabled(config={}, auto=False) is True

    def test_no_checkpoints_flag_disables(self, monkeypatch):
        """B4: run flag no_checkpoints=true disables the gate (active run)."""
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        assert (
            plan_checkpoint.is_checkpoint_enabled(
                config=None, auto=False, no_checkpoints=True
            )
            is False
        )


# ── TestRenderHtml ───────────────────────────────────────────────────────────


class TestRenderHtml:
    """_render_html: template structure, escaping, fields."""

    def test_contains_todo_id_in_title(self):
        html = plan_checkpoint._render_html(
            todo_id="TODO-0042",
            todo_content="some task",
            plan_content="plan body",
            port=12345,
        )
        assert "TODO-0042" in html
        assert "<title>" in html

    def test_contains_submit_url(self):
        """Form action must point to the local checkpoint server."""
        html = plan_checkpoint._render_html(
            todo_id="TODO-0001",
            todo_content="x",
            plan_content="",
            port=54321,
        )
        assert "http://127.0.0.1:54321/checkpoint" in html

    def test_escapes_html_in_todo(self):
        """XSS guard: <script> in TODO must be escaped in the form."""
        malicious = "<script>alert('xss')</script>"
        html = plan_checkpoint._render_html(
            todo_id="TODO-0001",
            todo_content=malicious,
            plan_content="",
            port=11111,
        )
        assert "<script>alert('xss')</script>" not in html
        assert "&lt;script&gt;" in html

    def test_has_three_action_buttons(self):
        """All three decisions (approve/edit/reject) must have buttons."""
        html = plan_checkpoint._render_html(
            todo_id="TODO-0001",
            todo_content="x",
            plan_content="",
            port=22222,
        )
        assert "submitDecision('approve')" in html
        assert "submitDecision('edit')" in html
        assert "submitDecision('reject')" in html

    def test_plan_placeholder_when_empty(self):
        """Empty plan_content renders a placeholder, not an empty div."""
        html = plan_checkpoint._render_html(
            todo_id="TODO-0001",
            todo_content="x",
            plan_content="",
            port=33333,
        )
        assert "(нет plan.md)" in html


# ── TestCheckpointServer ─────────────────────────────────────────────────────


class TestCheckpointServer:
    """_start_checkpoint_server: HTTP POST handling."""

    def _post(self, port: int, data: bytes) -> tuple[int, bytes]:
        """POST data to the checkpoint server, return (status, body)."""
        url = f"http://127.0.0.1:{port}/checkpoint"
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=2) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_approve_populates_decision(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=_free_port(), decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            status, body = self._post(
                server.server_address[1],
                b"decision=approve&token=" + FORM_TOKEN.encode(),
            )
            assert status == 200
            assert decision.get("decision") == "approve"
            assert edited == {}  # no edits for approve
        finally:
            server.shutdown()
            server.server_close()

    def test_edit_populates_edited_content(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=_free_port(), decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            new_content = "Rewritten TODO body"
            payload = (
                b"decision=edit&edited_content="
                + urllib.parse.quote(new_content).encode()
                + b"&token=" + FORM_TOKEN.encode()
            )
            status, _ = self._post(server.server_address[1], payload)
            assert status == 200
            assert decision.get("decision") == "edit"
            assert edited.get("content") == new_content
        finally:
            server.shutdown()
            server.server_close()

    def test_reject_populates_decision(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=_free_port(), decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            status, _ = self._post(
                server.server_address[1],
                b"decision=reject&token=" + FORM_TOKEN.encode(),
            )
            assert status == 200
            assert decision.get("decision") == "reject"
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_decision_returns_400(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=_free_port(), decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            status, _ = self._post(
                server.server_address[1],
                b"decision=hack&token=" + FORM_TOKEN.encode(),
            )
            assert status == 400
            assert decision == {}  # not populated
        finally:
            server.shutdown()
            server.server_close()


# ── TestRunPlanCheckpoint (end-to-end) ───────────────────────────────────────


class TestRunPlanCheckpoint:
    """run_plan_checkpoint: full flow (without browser; we POST directly)."""

    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        # QA (U7a review): point the checkpoint at a private tempdir. The
        # real tempdir is shared across xdist workers — parallel
        # run_plan_checkpoint tests create awf-checkpoint-TODO-*.html there
        # and test_temp_html_cleaned_up_after's before/after snapshot races
        # with their files (reproduced 3/3 on -n auto, incl. at baseline).
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def _make_project(self, tmp_path: Path, todo_content: str = "Original task") -> Path:
        """Create a minimal .agentic/ layout for checkpoint tests."""
        inbox = tmp_path / ".agentic" / "inbox"
        phases = tmp_path / ".agentic" / "phases"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        phases.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text(todo_content, encoding="utf-8")
        (phases / "plan.md").write_text("- [ ] Step 1\n- [ ] Step 2", encoding="utf-8")
        return tmp_path

    def test_no_todo_md_auto_approves(self, tmp_path, monkeypatch):
        """If TODO .md file is missing → auto-approve (nothing to preview)."""
        # No .agentic/inbox/TODO-0001.md
        (tmp_path / ".agentic" / "logs").mkdir(parents=True)
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", tmp_path, config=None,
            logs_dir=tmp_path / ".agentic" / "logs",
            timeout=1,
        )
        assert result == "approve"

    def test_non_utf8_todo_degrades_without_traceback(self, tmp_path, monkeypatch):
        """AUD03-07: non-UTF-8 TODO must not crash the checkpoint.

        Before the fix: UnicodeDecodeError escaped run_plan_checkpoint and
        killed the pipeline main-loop. Now the content is read with
        errors="replace" and the flow continues (here: plain timeout).
        """
        project = self._make_project(tmp_path)
        todo = project / ".agentic" / "inbox" / "TODO-0001.md"
        todo.write_bytes(b"\xff\xfe\x00corrupt-todo-bytes")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=1,
        )
        assert result == "timeout"

    def test_non_utf8_plan_md_degrades_without_traceback(self, tmp_path, monkeypatch):
        """AUD03-07: non-UTF-8 phases/plan.md must not crash the checkpoint."""
        project = self._make_project(tmp_path)
        (project / ".agentic" / "phases" / "plan.md").write_bytes(b"\xff\xfe\x00bad-plan")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=1,
        )
        assert result == "timeout"

    def test_approve_decision(self, tmp_path, monkeypatch):
        """User approves → returns 'approve', TODO untouched."""
        project = self._make_project(tmp_path)
        inbox = project / ".agentic" / "inbox"
        todo_md = inbox / "TODO-0001.md"
        original = todo_md.read_text(encoding="utf-8")

        decision_holder: dict = {}
        edited_holder: dict = {}
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        # Capture the port chosen by run_plan_checkpoint and POST to it.
        real_start = plan_checkpoint._start_checkpoint_server

        def capturing_start(port, decision_holder, edited_holder, **kw):
            decision_holder_ref = decision_holder
            edited_holder_ref = edited_holder
            server = real_start(port, decision_holder, edited_holder, **kw)
            # Schedule approve POST in background thread.
            import threading
            def _approve():
                time.sleep(0.2)  # let server bind + the form HTML land
                port_actual = server.server_address[1]
                token = _extract_form_token()
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port_actual}/checkpoint",
                    data=b"decision=approve&token=" + token.encode(),
                    timeout=2,
                ).read()
            threading.Thread(target=_approve, daemon=True).start()
            return server

        monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=5,
        )
        assert result == "approve"
        # TODO content unchanged
        assert todo_md.read_text(encoding="utf-8") == original

    def test_edit_decision_rewrites_todo(self, tmp_path, monkeypatch):
        """User edits → TODO .md rewritten with new content."""
        project = self._make_project(tmp_path, todo_content="Original")
        todo_md = project / ".agentic" / "inbox" / "TODO-0001.md"

        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        real_start = plan_checkpoint._start_checkpoint_server

        new_content = "Edited by user"

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)

            def _edit():
                time.sleep(0.2)  # let the form HTML land
                token = _extract_form_token()
                payload = (
                    b"decision=edit&edited_content="
                    + urllib.parse.quote(new_content).encode()
                    + b"&token=" + token.encode()
                )
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=payload,
                    timeout=2,
                ).read()

            import threading
            threading.Thread(target=_edit, daemon=True).start()
            return server

        monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=5,
        )
        assert result == "edit"
        assert todo_md.read_text(encoding="utf-8") == new_content

    def test_timeout_when_no_response(self, tmp_path, monkeypatch):
        """No POST arrives within timeout → returns 'timeout'."""
        project = self._make_project(tmp_path)
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=1,  # very short
        )
        assert result == "timeout"

    def test_temp_html_cleaned_up_after(self, tmp_path, monkeypatch):
        """HTML temp file must be deleted after checkpoint completes."""
        project = self._make_project(tmp_path)
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        # Snapshot temp dir before
        import tempfile
        tmp_dir = Path(tempfile.gettempdir())
        before = set(tmp_dir.glob("awf-checkpoint-TODO-*"))

        plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=1,  # timeout — but cleanup still runs
        )

        after = set(tmp_dir.glob("awf-checkpoint-TODO-*"))
        # No leftover temp files (any new ones created during the run are gone)
        new_leftovers = after - before
        assert not new_leftovers, f"Leftover temp files: {new_leftovers}"

    def test_empty_edit_does_not_overwrite_todo(self, tmp_path, monkeypatch):
        """BUG-3 fix: edit with empty textarea must NOT wipe the TODO file.

        User clicks Изменить → Подтвердить правки without typing anything.
        edited_content is "" — gate treats as approve (no-op), TODO is preserved.
        """
        project = self._make_project(tmp_path, todo_content="# Important task\ndo work")
        todo_md = project / ".agentic" / "inbox" / "TODO-0001.md"
        original = todo_md.read_text(encoding="utf-8")

        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        real_start = plan_checkpoint._start_checkpoint_server

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)

            def _empty_edit():
                time.sleep(0.2)  # let the form HTML land
                token = _extract_form_token()
                # Empty content submitted
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=b"decision=edit&edited_content=&token=" + token.encode(),
                    timeout=2,
                ).read()

            import threading
            threading.Thread(target=_empty_edit, daemon=True).start()
            return server

        monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs",
            timeout=5,
        )
        # Empty edit → returned as approve (no-op)
        assert result == "approve"
        # TODO file is UNCHANGED
        assert todo_md.read_text(encoding="utf-8") == original

    def test_webbrowser_open_failure_is_logged(self, tmp_path, monkeypatch):
        """BUG-4 fix: webbrowser.open raising must log a warning (not silent)."""
        project = self._make_project(tmp_path)
        logs_dir = project / ".agentic" / "logs"

        def _raise(*_a, **_kw):
            raise OSError("no browser available")

        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", _raise)

        plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=logs_dir, timeout=1,
        )

        # Verify the warning was written to orchestrator.log
        log_file = logs_dir / "orchestrator.log"
        log_content = log_file.read_text(encoding="utf-8")
        assert "webbrowser.open failed" in log_content
        assert "no browser available" in log_content


# ── TestCheckpointGateDispatch (orchestrator integration — T1) ───────────────


class TestCheckpointGateDispatch:
    """T1 fix: exercise orchestrator._run_plan_checkpoint_gate directly.

    The 31 tests above cover plan_checkpoint.py in isolation. These tests
    verify the dispatcher in orchestrator.py — the integration point
    where a wrong constant (e.g. 'reject' vs 'REJECT') would slip through.
    """

    def _make_project(self, tmp_path: Path) -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        phases = tmp_path / ".agentic" / "phases"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        phases.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub", encoding="utf-8")
        (phases / "plan.md").write_text("- [ ] Step 1", encoding="utf-8")
        return tmp_path

    def test_disabled_returns_zero(self, tmp_path, monkeypatch):
        """When checkpoint disabled (auto=True), gate returns 0 immediately."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)

        # Ensure run_plan_checkpoint is NEVER called
        def _fail_if_called(*_a, **_kw):
            raise AssertionError("run_plan_checkpoint must not be called when disabled")

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=True,  # disables checkpoint
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0

    def test_reject_returns_one(self, tmp_path, monkeypatch):
        """User rejects → gate returns 1 (pipeline must stop)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "reject",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 1

    def test_approve_returns_zero(self, tmp_path, monkeypatch):
        """User approves → gate returns 0 (pipeline continues)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "approve",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0

    def test_edit_returns_zero(self, tmp_path, monkeypatch):
        """User edits → gate returns 0 (pipeline continues with rewritten TODO)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "edit",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0

    def test_timeout_aborts_pipeline_dogfood3(self, tmp_path, monkeypatch):
        """Dogfood-3 regression: timeout must NOT auto-approve — abort pipeline.

        Old behavior: 'timeout' → return 0 (auto-approve, pipeline continues).
        Bug surfaced in ses_0423e4b1fffeLB7kYRCaKICWXa: user opened the form
        but didn't confirm immediately. Timeout fired → pipeline continued
        without explicit user approval. Supervisor misread log as
        'auto-decision approve' (technically correct, semantically wrong).

        Fix: timeout → return 1 (abort). User must re-run awf_start after
        explicit review.
        """
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "timeout",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        # Abort: rc=1 (was 0 — auto-approve — before fix)
        assert rc == 1, (
            "Dogfood-3: timeout must abort pipeline, not auto-approve. "
            "User must explicitly approve or re-run awf_start."
        )

        # Log records the abort
        log_text = (project / ".agentic" / "logs" / "orchestrator.log").read_text()
        assert "pipeline aborted" in log_text or "timed out" in log_text

    def test_env_override_disables_gate(self, tmp_path, monkeypatch):
        """AWF_PLAN_CHECKPOINT=false disables gate even when auto=False."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "false")

        # If gate tries to run, this would hang on real_form — make sure it doesn't.
        def _fail_if_called(*_a, **_kw):
            raise AssertionError("gate must short-circuit when env disables")

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0


# ── FU-06: gate wiring after todo resolution (AUD16-02) ─────────────────────


class TestGateWiringUnpinned:
    """AUD16-02: checkpoint gate must fire AFTER todo resolution.

    Unpinned start: current_todo="" enters the plan stage. The engine must
    resolve the dispatched TODO first and hand THAT id to the checkpoint
    gate. Before the fix (pre-FU-05 layout) the gate saw "" →
    run_plan_checkpoint("") → no file to preview → silent auto-approve and
    the form never opened (dogfood: 17/17 auto-approve).
    """

    def test_unpinned_plan_resolves_todo_before_gate(self, tmp_path, monkeypatch):
        from awf.pipeline import Stage
        from awf.pipeline_engine import execute_supervisor_stage

        inbox = tmp_path / ".agentic" / "inbox"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0007.md").write_text("# TODO-0007\ntask", encoding="utf-8")
        (inbox / "TODO-0007.ready").write_text("", encoding="utf-8")

        seen: list[str] = []
        def _gate(todo, p, c, a, ld):
            seen.append(todo)
            return 0

        monkeypatch.setattr(
            "awf.pipeline_engine._run_plan_checkpoint_gate", _gate,
        )
        monkeypatch.setattr(
            "awf.pipeline_engine._run_supervisor_stage",
            lambda *a, **kw: "",
        )

        stage = Stage(name="plan", role="supervisor", kind="plan")
        new_todo, _delta, rc = execute_supervisor_stage(
            stage, current_todo="", auto=True,
            project_dir=tmp_path, config={}, logs_dir=logs,
        )
        # Gate received the RESOLVED id, not the empty pre-stage value
        assert seen == ["TODO-0007"]
        assert new_todo == "TODO-0007"
        assert rc == 0


# ── FU-06: real port in state and logs (AUD03-02) ───────────────────────────


class TestCheckpointPortSurfaces:
    """AUD03-02: logs/state must carry the real OS-assigned port, not 0."""

    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        # A-03: the form HTML (with the one-time token) is rendered into the
        # tempdir — isolate it so the extraction glob sees only this test's
        # form (the real tempdir is shared across xdist workers).
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def _make_project(self, tmp_path: Path) -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        phases = tmp_path / ".agentic" / "phases"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        phases.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("task", encoding="utf-8")
        return tmp_path

    def test_state_and_log_carry_real_port(self, tmp_path, monkeypatch):
        from awf.pipeline_state import read_state

        project = self._make_project(tmp_path)
        logs_dir = project / ".agentic" / "logs"
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        real_ports: list[int] = []
        real_start = plan_checkpoint._start_checkpoint_server

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)
            real_ports.append(server.server_address[1])

            def _approve():
                time.sleep(0.2)  # let the form HTML land
                token = _extract_form_token()
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=b"decision=approve&token=" + token.encode(), timeout=2,
                ).read()

            import threading
            threading.Thread(target=_approve, daemon=True).start()
            return server

        monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None, logs_dir=logs_dir, timeout=5,
        )
        assert result == "approve"
        assert real_ports and real_ports[0] != 0

        state = read_state(project)
        assert state is not None
        # State must name the live port, not the 0 placeholder
        assert state["checkpoint_port"] == real_ports[0]

        log_text = (logs_dir / "orchestrator.log").read_text(encoding="utf-8")
        assert f"on port {real_ports[0]}" in log_text
        assert f"server_url=http://127.0.0.1:{real_ports[0]}" in log_text
        assert "on port 0" not in log_text
        assert "127.0.0.1:0" not in log_text


# ── FU-06: stale cleanup must not kill a live form (AUD03-03) ───────────────


class TestStaleCleanupProtectsLiveForm:
    """AUD03-03: cleanup must never delete the file a live checkpoint waits on.

    The 10-minute threshold is shorter than DEFAULT_CHECKPOINT_TIMEOUT
    (60 min): a form created 15 minutes ago by a live checkpoint used to be
    wiped by the next ``_cleanup_stale_temp_html`` call.
    """

    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        # QA: point cleanup at a private dir. The real tempdir is shared
        # across xdist workers — concurrent П7 tests there pre-clean or
        # unlink awf-checkpoint-*.html and would race with this test's files.
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def _old_form_file(self, name: str, age_seconds: int = 900) -> Path:
        path = Path(tempfile.gettempdir()) / name
        path.write_text("<html>form</html>", encoding="utf-8")
        old = time.time() - age_seconds
        import os
        os.utime(path, (old, old))
        return path

    def test_live_form_not_deleted_despite_age(self, tmp_path, monkeypatch):
        form = self._old_form_file("awf-checkpoint-TODO-0099-live.html")
        try:
            logs = tmp_path / ".agentic" / "logs"
            logs.mkdir(parents=True)
            from awf.pipeline_state import write_state

            write_state(
                tmp_path, logs_dir=logs,
                checkpoint_pending=True,
                checkpoint_form_url=f"file://{form}",
            )
            plan_checkpoint._cleanup_stale_temp_html(tmp_path)
            assert form.is_file(), "live form was deleted by stale cleanup"
        finally:
            form.unlink(missing_ok=True)

    def test_unreferenced_old_form_still_deleted(self, tmp_path, monkeypatch):
        form = self._old_form_file("awf-checkpoint-TODO-0098-dead.html")
        try:
            # No state at all → nothing is protected
            removed = plan_checkpoint._cleanup_stale_temp_html(tmp_path)
            assert removed >= 1
            assert not form.is_file()
        finally:
            form.unlink(missing_ok=True)

    def test_pending_false_releases_protection(self, tmp_path, monkeypatch):
        form = self._old_form_file("awf-checkpoint-TODO-0097-done.html")
        try:
            logs = tmp_path / ".agentic" / "logs"
            logs.mkdir(parents=True)
            from awf.pipeline_state import write_state

            write_state(
                tmp_path, logs_dir=logs,
                checkpoint_pending=False,
                checkpoint_form_url=f"file://{form}",
            )
            plan_checkpoint._cleanup_stale_temp_html(tmp_path)
            assert not form.is_file()
        finally:
            form.unlink(missing_ok=True)


# ── FU-06: hash-skip keyed by todo_id, post-edit content (AUD03-04) ─────────


class TestCheckpointHashSkip:
    """AUD03-04: skip hash must be keyed by todo_id and cover written content."""

    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        # A-03: the form HTML (with the one-time token) is rendered into the
        # tempdir — isolate it so _extract_form_token's glob sees only this
        # test's forms (the real tempdir is shared across xdist workers).
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def _make_project(self, tmp_path: Path, todo_id: str = "TODO-0001",
                      content: str = "Original task") -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        phases = tmp_path / ".agentic" / "phases"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        phases.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / f"{todo_id}.md").write_text(content, encoding="utf-8")
        return tmp_path

    def _run_with_post(self, tmp_path, todo_id, post_data: bytes, monkeypatch):
        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = _ORIGINAL_START_SERVER(port, decision_holder, edited_holder, **kw)

            def _post():
                time.sleep(0.2)  # let the form HTML land
                token = _extract_form_token(todo_id)
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=post_data + b"&token=" + token.encode(), timeout=2,
                ).read()

            import threading
            threading.Thread(target=_post, daemon=True).start()
            return server

        monkeypatch.setattr(plan_checkpoint, "_start_checkpoint_server", capturing_start)
        return plan_checkpoint.run_plan_checkpoint(
            todo_id, tmp_path, config=None,
            logs_dir=tmp_path / ".agentic" / "logs", timeout=5,
        )

    def test_edit_stores_hash_of_written_content(self, tmp_path, monkeypatch):
        """After decision=edit the stored hash must cover the NEW content."""
        import hashlib

        project = self._make_project(tmp_path, content="Original")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        new_content = "Edited by user"
        payload = (
            b"decision=edit&edited_content="
            + urllib.parse.quote(new_content).encode()
        )
        assert self._run_with_post(project, "TODO-0001", payload, monkeypatch) == "edit"

        hash_file = project / ".agentic" / "context" / "checkpoint-approved.hash"
        stored = hash_file.read_text(encoding="utf-8").strip()
        expected_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()[:16]
        assert stored == f"TODO-0001:{expected_hash}", (
            f"stored {stored!r} — must be todo_id:sha256(written content)"
        )

    def test_reskip_after_edit_same_content(self, tmp_path, monkeypatch):
        """kill+start right after an edit must NOT re-open the form."""
        project = self._make_project(tmp_path, content="Original")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        new_content = "Edited by user"
        payload = (
            b"decision=edit&edited_content="
            + urllib.parse.quote(new_content).encode()
        )
        assert self._run_with_post(project, "TODO-0001", payload, monkeypatch) == "edit"

        opened: list[str] = []
        monkeypatch.setattr(
            plan_checkpoint.webbrowser, "open",
            lambda url, **_kw: opened.append(url),
        )
        # Second run, same (now on-disk) content → hash-skip, no form
        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=5,
        )
        assert result == "approve"
        assert opened == [], "form re-opened for just-approved content"

    def test_different_todo_same_content_opens_form(self, tmp_path, monkeypatch):
        """Same text under a different todo_id is a new plan — no silent skip."""
        project = self._make_project(tmp_path, todo_id="TODO-0001", content="Shared text")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)
        assert self._run_with_post(project, "TODO-0001", b"decision=approve", monkeypatch) == "approve"

        (project / ".agentic" / "inbox" / "TODO-0002.md").write_text(
            "Shared text", encoding="utf-8",
        )
        opened: list[str] = []
        monkeypatch.setattr(
            plan_checkpoint.webbrowser, "open",
            lambda url, **_kw: opened.append(url),
        )

        # The second run MUST open the form (no hash-skip across todo ids).
        # _run_with_post patches the server start and answers with approve.
        result = self._run_with_post(
            project, "TODO-0002", b"decision=approve", monkeypatch,
        )
        assert result == "approve"
        assert opened, "form was skipped for a different todo_id with same text"


# ── FU-06: first-wins on concurrent decisions (AUD03-05) ────────────────────


class TestCheckpointFirstWins:
    """AUD03-05 + A-03: the first accepted (valid-token) POST decides;
    repeat/racing POSTs are refused (spent token), never overwrite."""

    def _post(self, port: int, data: bytes) -> int:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/checkpoint", data=data, timeout=2,
            ) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_second_post_does_not_change_decision(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=0, decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            port = server.server_address[1]
            tok = b"&token=" + FORM_TOKEN.encode()
            assert self._post(port, b"decision=approve" + tok) == 200
            # A-03: the token is one-time — the repeat is refused (403),
            # and the decision is not overwritten either way.
            assert self._post(port, b"decision=reject" + tok) == 403
            assert decision.get("decision") == "approve", (
                f"first-wins broken: holder={decision}"
            )
        finally:
            server.shutdown()
            server.server_close()

    def test_edit_then_approve_keeps_edit_content(self):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=0, decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            port = server.server_address[1]
            tok = b"&token=" + FORM_TOKEN.encode()
            payload = b"decision=edit&edited_content=First+content" + tok
            assert self._post(port, payload) == 200
            assert self._post(port, b"decision=approve" + tok) == 403
            assert decision.get("decision") == "edit"
            assert edited.get("content") == "First content"
        finally:
            server.shutdown()
            server.server_close()

    def test_concurrent_posts_yield_single_consistent_decision(self):
        """Two racing POSTs with the same valid token: exactly one is
        accepted, the other is refused (spent token), one decision lands."""
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=0, decision_holder=decision, edited_holder=edited,
            token=FORM_TOKEN,
        )
        try:
            import threading

            port = server.server_address[1]
            bar = threading.Barrier(2)
            statuses: list[int] = []

            def _post(payload: bytes):
                bar.wait()
                statuses.append(self._post(port, payload))

            tok = b"&token=" + FORM_TOKEN.encode()
            t1 = threading.Thread(target=_post, args=(b"decision=approve" + tok,))
            t2 = threading.Thread(target=_post, args=(b"decision=reject" + tok,))
            t1.start(); t2.start(); t1.join(); t2.join()

            assert sorted(statuses) == [200, 403], (
                f"expected one accepted and one refused, got {statuses}"
            )
            assert list(decision) == ["decision"]
            assert decision["decision"] in ("approve", "reject")
            assert edited == {}, "non-edit decision must not leave edited content"
        finally:
            server.shutdown()
            server.server_close()


# ── B1: POST persists the decision to disk (survives pipeline death) ───────


class TestCheckpointDecisionFile:
    """B1: the first accepted POST writes CHECKPOINT-<todo>.json atomically.

    The file is what survives when the pipeline process dies after the
    submit (timeout teardown race, crash). The live path still applies the
    in-memory decision; the file is the backup for the next gate entry.
    """

    def _post(self, port: int, data: bytes) -> int:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/checkpoint", data=data, timeout=2,
            ) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def _server(self, tmp_path: Path, decision_file: Path | None):
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=0,
            decision_holder=decision,
            edited_holder=edited,
            decision_file=decision_file,
            todo_id="TODO-0001" if decision_file is not None else None,
            logs_dir=tmp_path / "logs",
            token=FORM_TOKEN,
        )
        return server, decision, edited

    def test_approve_persists_decision_file(self, tmp_path):
        decision_file = tmp_path / "CHECKPOINT-TODO-0001.json"
        server, decision, _ = self._server(tmp_path, decision_file)
        try:
            port = server.server_address[1]
            assert self._post(port, b"decision=approve&token=" + FORM_TOKEN.encode()) == 200
            assert decision["decision"] == "approve"
            data = json.loads(decision_file.read_text(encoding="utf-8"))
            assert data["decision"] == "approve"
            assert data["todo_id"] == "TODO-0001"
            assert data["content"] == ""
            assert data["submitted_at"]
        finally:
            server.shutdown()
            server.server_close()

    def test_edit_persists_edited_content(self, tmp_path):
        decision_file = tmp_path / "CHECKPOINT-TODO-0001.json"
        server, _, edited = self._server(tmp_path, decision_file)
        try:
            port = server.server_address[1]
            payload = (
                b"decision=edit&edited_content="
                + urllib.parse.quote("Edited by user").encode()
                + b"&token=" + FORM_TOKEN.encode()
            )
            assert self._post(port, payload) == 200
            assert edited["content"] == "Edited by user"
            data = json.loads(decision_file.read_text(encoding="utf-8"))
            assert data["decision"] == "edit"
            assert data["content"] == "Edited by user"
        finally:
            server.shutdown()
            server.server_close()

    def test_second_post_keeps_first_decision_on_disk(self, tmp_path):
        """AUD03-05 + A-03 parity: the disk file keeps the first decision —
        the repeat is refused (spent token), not overwritten."""
        decision_file = tmp_path / "CHECKPOINT-TODO-0001.json"
        server, _, _ = self._server(tmp_path, decision_file)
        try:
            port = server.server_address[1]
            tok = b"&token=" + FORM_TOKEN.encode()
            assert self._post(port, b"decision=approve" + tok) == 200
            assert self._post(port, b"decision=reject" + tok) == 403
            data = json.loads(decision_file.read_text(encoding="utf-8"))
            assert data["decision"] == "approve"
        finally:
            server.shutdown()
            server.server_close()

    def test_no_file_when_not_configured(self, tmp_path):
        """Calls without a decision_file must not create the file."""
        server, _, _ = self._server(tmp_path, None)
        try:
            port = server.server_address[1]
            assert (
                self._post(port, b"decision=approve&token=" + FORM_TOKEN.encode())
                == 200
            )
            assert not list(tmp_path.glob("CHECKPOINT-*.json"))
        finally:
            server.shutdown()
            server.server_close()


# ── B1: saved decision is applied on the next gate entry ────────────────────


class TestCheckpointDecisionSurvives:
    """B1: an unconsumed saved decision is applied without opening the form.

    Scenario: the pipeline died after the user's submit (timeout teardown
    race). On the next entry the gate must apply the saved decision instead
    of re-asking, and consume it so it is not applied twice.
    """

    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def _make_project(self, tmp_path: Path, content: str = "Original task") -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text(content, encoding="utf-8")
        return tmp_path

    def _save_decision(
        self, project: Path, decision: str, content: str = ""
    ) -> Path:
        f = project / ".agentic" / "context" / "CHECKPOINT-TODO-0001.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(
                {
                    "decision": decision,
                    "todo_id": "TODO-0001",
                    "content": content,
                    "submitted_at": "2026-09-22T12:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        return f

    def test_saved_approve_applies_without_form(self, tmp_path, monkeypatch):
        project = self._make_project(tmp_path)
        self._save_decision(project, "approve")
        opened: list[str] = []
        monkeypatch.setattr(
            plan_checkpoint.webbrowser, "open",
            lambda url, **_kw: opened.append(url),
        )

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "approve"
        assert opened == [], "form must not open when a saved decision applies"
        f = project / ".agentic" / "context" / "CHECKPOINT-TODO-0001.json"
        assert not f.is_file()
        assert f.with_name(f.name + ".consumed").is_file()

    def test_saved_edit_rewrites_todo_and_consumes(self, tmp_path, monkeypatch):
        project = self._make_project(tmp_path, content="Original")
        self._save_decision(project, "edit", content="Edited by user")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "edit"
        assert (
            (project / ".agentic" / "inbox" / "TODO-0001.md").read_text(encoding="utf-8")
            == "Edited by user"
        )
        f = project / ".agentic" / "context" / "CHECKPOINT-TODO-0001.json"
        assert not f.is_file()

    def test_saved_reject_returns_reject(self, tmp_path, monkeypatch):
        project = self._make_project(tmp_path)
        self._save_decision(project, "reject")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "reject"
        f = project / ".agentic" / "context" / "CHECKPOINT-TODO-0001.json"
        assert not f.is_file()

    def test_consumed_decision_not_applied_twice(self, tmp_path, monkeypatch):
        project = self._make_project(tmp_path)
        self._save_decision(project, "approve")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        assert plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        ) == "approve"

        # Second entry: no saved decision left → the form opens as before.
        opened: list[str] = []
        monkeypatch.setattr(
            plan_checkpoint.webbrowser, "open",
            lambda url, **_kw: opened.append(url),
        )
        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )
        assert result == "timeout"
        assert opened, "form must re-open when no saved decision remains"

    def test_empty_saved_edit_treated_as_approve(self, tmp_path, monkeypatch):
        """BUG-3 parity: an empty edit content must never wipe the TODO."""
        project = self._make_project(tmp_path, content="# Important\ndo work")
        todo_md = project / ".agentic" / "inbox" / "TODO-0001.md"
        self._save_decision(project, "edit", content="   ")
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "approve"
        assert todo_md.read_text(encoding="utf-8") == "# Important\ndo work"

    def test_corrupt_decision_file_opens_form(self, tmp_path, monkeypatch):
        """Garbage on disk degrades to a fresh form (never auto-approve)."""
        project = self._make_project(tmp_path)
        f = project / ".agentic" / "context" / "CHECKPOINT-TODO-0001.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not-json{{", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr(
            plan_checkpoint.webbrowser, "open",
            lambda url, **_kw: opened.append(url),
        )

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "timeout"
        assert opened, "form must open when the saved decision is corrupt"
        assert f.is_file(), "corrupt file is kept for inspection"

    def test_timeout_log_mentions_saved_path_and_continue(self, tmp_path, monkeypatch):
        """B1: the timeout log tells the owner where the decision lands and
        that `awf continue` picks it up (policy: timeout still aborts)."""
        project = self._make_project(tmp_path)
        monkeypatch.setattr(plan_checkpoint.webbrowser, "open", lambda *_a, **_kw: None)

        result = plan_checkpoint.run_plan_checkpoint(
            "TODO-0001", project, config=None,
            logs_dir=project / ".agentic" / "logs", timeout=1,
        )

        assert result == "timeout"
        log_text = (
            project / ".agentic" / "logs" / "orchestrator.log"
        ).read_text(encoding="utf-8")
        assert "CHECKPOINT-TODO-0001.json" in log_text
        assert "awf continue" in log_text


# ── B4: engine gate honors the active run's no_checkpoints flag ─────────────


class TestNoCheckpointsGate:
    """B4: _run_plan_checkpoint_gate reads the flag from the RUN STATE."""

    def _make_project(self, tmp_path: Path) -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub", encoding="utf-8")
        return tmp_path

    def test_active_run_flag_skips_gate(self, tmp_path, monkeypatch):
        from awf import run_state
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        # Without delenv the session-wide AWF_PLAN_CHECKPOINT=false (conftest)
        # disables the gate and the test would pass even without the flag.
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        project = self._make_project(tmp_path)
        run_state.write_run(
            project, active=True, queue=["TODO-0001"], no_checkpoints=True,
        )

        def _fail_if_called(*_a, **_kw):
            raise AssertionError(
                "run_plan_checkpoint must not be called when no_checkpoints=true"
            )

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0
        log_text = (
            project / ".agentic" / "logs" / "orchestrator.log"
        ).read_text(encoding="utf-8")
        assert "no_checkpoints" in log_text

    def test_inactive_run_flag_does_not_skip(self, tmp_path, monkeypatch):
        """A finished run's flag must not skip the checkpoint of a later
        manual start — only an ACTIVE run carries the flag."""
        from awf import run_state
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        run_state.write_run(
            project, active=False, queue=["TODO-0001"], no_checkpoints=True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "approve",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0

    def test_no_run_state_behaves_as_before(self, tmp_path, monkeypatch):
        """Without any run state the gate is unchanged (form path)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        project = self._make_project(tmp_path)
        monkeypatch.setattr(
            "awf.plan_checkpoint.is_checkpoint_enabled", lambda *_a, **_kw: True,
        )
        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint",
            lambda *_a, **_kw: "approve",
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0


# ── RUN3 #6: no_checkpoints on a single start/continue launch ───────────────


class TestLaunchNoCheckpoints:
    """RUN3 #6: the single-launch no_checkpoints parameter.

    Transport: AWF_NO_CHECKPOINTS env for the pipeline process's duration
    (background: child env at spawn; foreground: set around run_pipeline,
    restored on every exit path). The gate combines it with the B4 run
    flag (OR) — the config/env/auto bypasses stay in is_checkpoint_enabled.
    """

    def _make_project(self, tmp_path: Path) -> Path:
        inbox = tmp_path / ".agentic" / "inbox"
        logs = tmp_path / ".agentic" / "logs"
        inbox.mkdir(parents=True)
        logs.mkdir(parents=True)
        (inbox / "TODO-0001.md").write_text("# TODO-0001\nstub", encoding="utf-8")
        return tmp_path

    @pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", " Yes "])
    def test_helper_truthy(self, monkeypatch, val):
        monkeypatch.setenv("AWF_NO_CHECKPOINTS", val)
        assert plan_checkpoint.launch_no_checkpoints() is True

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "2"])
    def test_helper_falsy(self, monkeypatch, val):
        monkeypatch.setenv("AWF_NO_CHECKPOINTS", val)
        assert plan_checkpoint.launch_no_checkpoints() is False

    def test_helper_unset(self, monkeypatch):
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
        assert plan_checkpoint.launch_no_checkpoints() is False

    def test_launch_flag_skips_gate(self, tmp_path, monkeypatch):
        """Env set, no run state → gate skips, the form never opens, and
        the log carries the RUN3-6 line (dogfood evidence)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.setenv("AWF_NO_CHECKPOINTS", "1")
        project = self._make_project(tmp_path)

        def _fail_if_called(*_a, **_kw):
            raise AssertionError("run_plan_checkpoint must not open the form")

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0
        log_text = (
            project / ".agentic" / "logs" / "orchestrator.log"
        ).read_text(encoding="utf-8")
        assert "start no_checkpoints=true" in log_text

    def test_without_flag_form_opens_as_before(self, tmp_path, monkeypatch):
        """No env, no run state → the gate runs the form path (unchanged)."""
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
        project = self._make_project(tmp_path)
        calls: list = []

        def _fake_form(*_a, **_kw):
            calls.append(1)
            return "approve"

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fake_form,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0
        assert calls, "form path must run without the launch flag"

    def test_launch_flag_wins_over_inactive_run(self, tmp_path, monkeypatch):
        """OR semantics: a FINISHED run's flag is ignored (B4 rule), but the
        launch parameter still skips the gate."""
        from awf import run_state
        from awf.pipeline_engine import _run_plan_checkpoint_gate

        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.setenv("AWF_NO_CHECKPOINTS", "true")
        project = self._make_project(tmp_path)
        run_state.write_run(
            project, active=False, queue=["TODO-0001"], no_checkpoints=True,
        )

        def _fail_if_called(*_a, **_kw):
            raise AssertionError("run_plan_checkpoint must not open the form")

        monkeypatch.setattr(
            "awf.plan_checkpoint.run_plan_checkpoint", _fail_if_called,
        )

        rc = _run_plan_checkpoint_gate(
            current_todo="TODO-0001",
            project_dir=project,
            config={},
            auto=False,
            logs_dir=project / ".agentic" / "logs",
        )
        assert rc == 0


class TestLaunchNoCheckpointsPlumbing:
    """start/continue pass the param to the pipeline process as env, and the
    flag does not survive the launch: parent env, state files and config
    stay clean, the next launch carries nothing."""

    def _setup_project(self, tmp_path: Path) -> Path:
        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / ".agentic").mkdir()
        (proj / ".agentic" / "config.yaml").write_text(
            "project:\n  name: test\n", encoding="utf-8"
        )
        inbox = proj / ".agentic" / "inbox"
        inbox.mkdir()
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task", encoding="utf-8")
        return proj

    def _capture_popen(self, monkeypatch):
        """Fake Popen at the background launcher — captures child argv/env.

        Also mocks the child liveness check (DF5-10) so tests don't sleep
        on a fake PID.
        """
        captured: dict = {}

        class _CapturingPopen:
            pid = 2**31  # outside the OS range — never alive (probe_alive)

            def __init__(self, args, **kwargs):
                captured["argv"] = list(args)
                captured["env"] = dict(kwargs.get("env") or {})

        from awf.api import _background
        monkeypatch.setattr(_background.subprocess, "Popen", _CapturingPopen)
        monkeypatch.setattr(
            "awf.api.pipeline._verify_child_alive",
            lambda pid, log_file=None: True,
        )
        return captured

    def test_start_background_sets_child_env(self, tmp_path, monkeypatch):
        """awf_start(no_checkpoints=True, background) → child env has the
        flag; the parent process env is untouched."""
        import os

        from awf import api

        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

        result = api.start_pipeline(proj, background=True, no_checkpoints=True)
        assert result.run_mode == "background"
        assert captured["env"].get("AWF_NO_CHECKPOINTS") == "1"
        assert captured["env"].get("AWF_BACKGROUND_CHILD") == "1"
        assert "AWF_NO_CHECKPOINTS" not in os.environ

    def test_start_background_second_launch_not_sticky(self, tmp_path, monkeypatch):
        """The flag dies with the launch: the NEXT background start (without
        the param) must not carry it."""
        from awf import api

        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

        r1 = api.start_pipeline(proj, background=True, no_checkpoints=True)
        env1 = dict(captured["env"])
        assert r1.run_mode == "background"
        assert env1.get("AWF_NO_CHECKPOINTS") == "1"

        r2 = api.start_pipeline(proj, background=True)
        env2 = dict(captured["env"])
        assert r2.run_mode == "background"
        assert "AWF_NO_CHECKPOINTS" not in env2

    def test_continue_background_sets_child_env(self, tmp_path, monkeypatch):
        """awf_continue(no_checkpoints=True, background) → child env has it."""
        from awf import api

        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

        result = api.continue_pipeline(proj, background=True, no_checkpoints=True)
        assert result.run_mode == "background"
        assert captured["env"].get("AWF_NO_CHECKPOINTS") == "1"

    def test_continue_background_without_flag_clean(self, tmp_path, monkeypatch):
        """continue without the param → child env has no AWF_NO_CHECKPOINTS."""
        from awf import api

        proj = self._setup_project(tmp_path)
        captured = self._capture_popen(monkeypatch)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

        result = api.continue_pipeline(proj, background=True)
        assert result.run_mode == "background"
        assert "AWF_NO_CHECKPOINTS" not in captured["env"]

    def test_start_foreground_env_set_during_restored_after(self, tmp_path, monkeypatch):
        """Foreground: the env is visible DURING the real run_pipeline (the
        gate reads it) and restored on the clean exit path — the next
        in-process launch starts from a clean environment. The supervisor
        stage is short-circuited (no opencode subprocess in unit tests)."""
        import os

        from awf import api

        proj = self._setup_project(tmp_path)
        pipes = proj / ".agentic" / "pipelines"
        pipes.mkdir()
        (pipes / "default.yaml").write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
        monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)

        seen: dict = {}

        def _fake_supervisor_stage(
            stage, current_todo, auto, project_dir, config, logs_dir,
            pipeline_name=None,
        ):
            seen["env"] = os.environ.get("AWF_NO_CHECKPOINTS")
            return current_todo, 1, 0

        monkeypatch.setattr(
            "awf.orchestrator.execute_supervisor_stage", _fake_supervisor_stage,
        )

        result = api.start_pipeline(proj, background=False, no_checkpoints=True)
        assert result.run_mode == "foreground"
        assert result.exit_code == 0
        assert seen["env"] == "1"
        assert "AWF_NO_CHECKPOINTS" not in os.environ

    def test_start_foreground_without_flag_refused_as_before(self, tmp_path, monkeypatch):
        """Guard unchanged (DF6-5): foreground + enabled checkpoint + no
        param → the refusal, run_pipeline never reached."""
        from awf import api

        proj = self._setup_project(tmp_path)
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)
        monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)

        def _must_not_run(_args):
            raise AssertionError("refusal must happen before run_pipeline")

        monkeypatch.setattr("awf.orchestrator.run_pipeline", _must_not_run)

        result = api.start_pipeline(proj, background=False)
        assert result.run_mode == "noop"
        assert result.exit_code == 1
        assert "checkpoint" in result.message.lower()

    def test_launch_flag_not_written_to_state_or_config(self, tmp_path, monkeypatch):
        """Process-scoped: config.yaml and every state file must not gain a
        no_checkpoints key, and a single launch creates no run state."""
        from awf import api

        proj = self._setup_project(tmp_path)
        self._capture_popen(monkeypatch)
        monkeypatch.delenv("AWF_NO_CHECKPOINTS", raising=False)

        config_before = (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8")
        api.start_pipeline(proj, background=True, no_checkpoints=True)

        assert (proj / ".agentic" / "config.yaml").read_text(encoding="utf-8") == config_before
        state_dir = proj / ".agentic" / "state"
        if state_dir.is_dir():
            for f in state_dir.glob("*"):
                if f.is_file():
                    text = f.read_text(encoding="utf-8", errors="replace")
                    assert "no_checkpoints" not in text, f.name
        assert not (proj / ".agentic" / "state" / "run.yaml").is_file()


# ── Helpers ──────────────────────────────────────────────────────────────────
# _free_port — imported from the root conftest (AUD12-09).
