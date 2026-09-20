"""BD-36: Plan checkpoint integration tests.

Coverage matrix:
- TestCheckpointEnabled — bypass logic (auto / env / config)
- TestRenderHtml — template structure, escaping, fields
- TestCheckpointServer — HTTP server POST handling
- TestRunPlanCheckpoint — end-to-end flow (no browser; uses HTTP POST)
"""
from __future__ import annotations

import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from awf import plan_checkpoint

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
        )
        try:
            status, body = self._post(
                server.server_address[1], b"decision=approve",
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
        )
        try:
            new_content = "Rewritten TODO body"
            payload = (
                b"decision=edit&edited_content="
                + urllib.parse.quote(new_content).encode()
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
        )
        try:
            status, _ = self._post(
                server.server_address[1], b"decision=reject",
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
        )
        try:
            status, _ = self._post(
                server.server_address[1], b"decision=hack",
            )
            assert status == 400
            assert decision == {}  # not populated
        finally:
            server.shutdown()
            server.server_close()


# ── TestRunPlanCheckpoint (end-to-end) ───────────────────────────────────────


class TestRunPlanCheckpoint:
    """run_plan_checkpoint: full flow (without browser; we POST directly)."""

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

    def test_non_utf8_brief_degrades_without_traceback(self, tmp_path, monkeypatch):
        """AUD03-07: non-UTF-8 BRIEF must not crash the checkpoint.

        Before the fix: UnicodeDecodeError escaped run_plan_checkpoint and
        killed the pipeline main-loop. Now the content is read with
        errors="replace" and the flow continues (here: plain timeout).
        """
        project = self._make_project(tmp_path)
        brief = project / ".agentic" / "inbox" / "BRIEF-TODO-0001.md"
        brief.write_bytes(b"\xff\xfe\x00corrupt-brief-bytes")
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

        def capturing_start(port, decision_holder, edited_holder):
            decision_holder_ref = decision_holder
            edited_holder_ref = edited_holder
            server = real_start(port, decision_holder, edited_holder)
            # Schedule approve POST in background thread.
            import threading
            def _approve():
                time.sleep(0.2)  # let server fully bind
                port_actual = server.server_address[1]
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port_actual}/checkpoint",
                    data=b"decision=approve",
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

        def capturing_start(port, decision_holder, edited_holder):
            server = real_start(port, decision_holder, edited_holder)

            def _edit():
                time.sleep(0.2)
                payload = (
                    b"decision=edit&edited_content="
                    + urllib.parse.quote(new_content).encode()
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

        def capturing_start(port, decision_holder, edited_holder):
            server = real_start(port, decision_holder, edited_holder)

            def _empty_edit():
                time.sleep(0.2)
                # Empty content submitted
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=b"decision=edit&edited_content=",
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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Find a free port (different from any in-use one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
