"""BD-36: Plan checkpoint integration tests.

Coverage matrix:
- TestCheckpointEnabled — bypass logic (auto / env / config)
- TestRenderHtml — template structure, escaping, fields
- TestCheckpointServer — HTTP server POST handling
- TestRunPlanCheckpoint — end-to-end flow (no browser; uses HTTP POST)
"""
from __future__ import annotations

import socket
import tempfile
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

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)

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

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)

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
                time.sleep(0.2)
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=b"decision=approve", timeout=2,
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
        real_start = plan_checkpoint._start_checkpoint_server

        def capturing_start(port, decision_holder, edited_holder, **kw):
            server = real_start(port, decision_holder, edited_holder, **kw)

            def _post():
                time.sleep(0.2)
                urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_address[1]}/checkpoint",
                    data=post_data, timeout=2,
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
    """AUD03-05: the first accepted POST decides; later POSTs ack, not overwrite."""

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
        )
        try:
            port = server.server_address[1]
            assert self._post(port, b"decision=approve") == 200
            assert self._post(port, b"decision=reject") == 200  # ack, no overwrite
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
        )
        try:
            port = server.server_address[1]
            payload = b"decision=edit&edited_content=First+content"
            assert self._post(port, payload) == 200
            assert self._post(port, b"decision=approve") == 200
            assert decision.get("decision") == "edit"
            assert edited.get("content") == "First content"
        finally:
            server.shutdown()
            server.server_close()

    def test_concurrent_posts_yield_single_consistent_decision(self):
        """Two racing POSTs: exactly one decision lands, holders stay in sync."""
        decision: dict = {}
        edited: dict = {}
        server = plan_checkpoint._start_checkpoint_server(
            port=0, decision_holder=decision, edited_holder=edited,
        )
        try:
            import threading

            port = server.server_address[1]
            bar = threading.Barrier(2)

            def _post(payload: bytes):
                bar.wait()
                self._post(port, payload)

            t1 = threading.Thread(target=_post, args=(b"decision=approve",))
            t2 = threading.Thread(target=_post, args=(b"decision=reject",))
            t1.start(); t2.start(); t1.join(); t2.join()

            assert list(decision) == ["decision"]
            assert decision["decision"] in ("approve", "reject")
            assert edited == {}, "non-edit decision must not leave edited content"
        finally:
            server.shutdown()
            server.server_close()


# ── Helpers ──────────────────────────────────────────────────────────────────


def _free_port() -> int:
    """Find a free port (different from any in-use one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
