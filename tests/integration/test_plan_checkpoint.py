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


# ── Helpers ──────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Find a free port (different from any in-use one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
