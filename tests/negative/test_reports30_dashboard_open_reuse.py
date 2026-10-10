"""REPORTS30: the dashboard must NOT open a new tab when the launch reuses
the previous server (owner's backlog 09.10, after TODO-0174).

REPORTS29 port reuse works: start/continue/run_next rebind the previous
launch's port, so the old browser tab keeps its URL and shows the new
run. But every background launch unconditionally called
_open_dashboard_sync, which opened a SECOND tab on top of the live one.

Invariant (TODO-0184):
1. The engine (start_dashboard_with_reuse) writes a boot marker
   state/dashboard_boot (pid + ts) on an ACTUAL start — a new port
   (first launch, or fallback over a foreign-held port). On a port
   reuse the marker is NOT updated.
2. The plugin (_open_dashboard_sync, wait=True — just launched) opens a
   tab only when the marker is fresh relative to THIS launch (marker ts
   at/after the launch start, minus a small slop). A stale marker means
   the port was reused and a live tab from a previous launch still
   points at the same URL → opened=False, the URL is returned,
   webbrowser.open is NOT called.
3. No marker / corrupt marker → open conservatively (first-launch
   behavior). The explicit open tool (wait=False) always opens.
"""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from agent_workflow_ui.tools import awf

from awf import api


def _port_file(project: Path) -> Path:
    return project / ".agentic" / "state" / "dashboard_port"


def _marker_file(project: Path) -> Path:
    return project / ".agentic" / "state" / "dashboard_boot"


def _read_port(project: Path) -> int:
    return int(_port_file(project).read_text(encoding="utf-8").strip())


def _write_port(project: Path, port: int) -> None:
    f = _port_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(port), encoding="utf-8")


def _write_marker(project: Path, ts: float, pid: int = 4242) -> None:
    f = _marker_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"pid": pid, "ts": ts}), encoding="utf-8")


def _read_marker(project: Path) -> dict:
    return json.loads(_marker_file(project).read_text(encoding="utf-8"))


@pytest.fixture
def project(tmp_git_repo):
    """Project with git + .agentic/ + plan→worker→verify pipeline."""
    repo = tmp_git_repo
    api.init_project(repo, project_name="DashOpenReuse")

    pipes = repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "plan"\n    role: "supervisor"\n'
        '  - name: "worker"\n    role: "worker"\n'
        '    on_blocked: "escalate"\n    max_retries: 3\n'
        '  - name: "verify"\n    role: "supervisor"\n'
        '    on_approved: "commit_and_next"\n    on_rejected: "replan"\n',
        encoding="utf-8",
    )

    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute TODO.\n", encoding="utf-8")
    return repo


def _create_todo(project: Path, todo_id: str) -> None:
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\ntask\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True,
    ).stdout.strip()
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{todo_id}.sha").write_text(sha + "\n", encoding="utf-8")


def _make_args(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        project_dir=str(project),
        pipeline=None,
        from_stage=None,
        auto=True,
        timeout=None,
    )


def _mock_stages(monkeypatch, project: Path) -> None:
    """Mock supervisor + agent stages — no real opencode subprocess, but
    the orchestrator's dashboard lifecycle is the REAL code path (real
    bind on loopback, real port file, real exit path)."""

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        if stage.kind == "verify" and todo_id:
            inbox_path = project_dir / ".agentic" / "inbox"
            inbox_path.mkdir(parents=True, exist_ok=True)
            (inbox_path / f"ACK-{todo_id}.ready").write_text("", encoding="utf-8")
            (inbox_path / f"APPROVE-{todo_id}.ready").write_text("", encoding="utf-8")
            return f"ACK-{todo_id}"
        return ""

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, prev_handoffs=None,
                   hard_timeout=None, retry_note=None, attempt=1, **kw):
        outbox = project_dir / ".agentic" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"DONE-{todo_id}.md").write_text(f"# Done\n{todo_id}\n", encoding="utf-8")
        (outbox / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")
        (project_dir / "src.txt").write_text("worker output\n", encoding="utf-8")

    import awf.pipeline_engine as engine
    monkeypatch.setattr(engine, "_run_supervisor_stage", mock_supervisor)
    monkeypatch.setattr(engine, "_run_agent_stage", mock_agent)


class TestBootMarkerEngine:
    """Invariant 1: the marker marks an ACTUAL start (a new port) and is
    left untouched on a port reuse."""

    def test_actual_start_writes_boot_marker(self, project, monkeypatch):
        """A first launch (no port file) is an actual start — the marker
        appears with pid + ts once the pipeline exits."""
        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        assert run_pipeline(_make_args(project)) == 0

        assert _marker_file(project).is_file(), (
            "no state/dashboard_boot after an actual start — the plugin "
            "cannot tell a fresh server from a reused one"
        )
        data = _read_marker(project)
        assert int(data["pid"]) > 0
        assert abs(time.time() - float(data["ts"])) < 300, "marker ts implausible"

    def test_reuse_does_not_update_boot_marker(self, project, monkeypatch):
        """The second launch rebinds the first's port (REPORTS29) — the
        marker must stay byte-identical: the plugin reads 'stale' and
        skips the open."""
        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        assert run_pipeline(_make_args(project)) == 0
        raw1 = _marker_file(project).read_text(encoding="utf-8")
        port1 = _read_port(project)

        _create_todo(project, "TODO-0002")
        assert run_pipeline(_make_args(project)) == 0
        assert _read_port(project) == port1, (
            "precondition broken: the second launch must reuse the port "
            "(REPORTS29 invariant 1)"
        )
        raw2 = _marker_file(project).read_text(encoding="utf-8")
        assert raw2 == raw1, (
            f"marker changed on a port reuse: {raw1!r} -> {raw2!r} — the "
            "plugin would see a fresh marker and open a second tab"
        )

    def test_fallback_start_updates_boot_marker(self, project, monkeypatch):
        """A port held by a FOREIGN process → fallback to a new port = an
        actual start again → the marker gets a new ts (any old tab
        pointed at the foreign port, not this server)."""
        foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen(1)
        busy = foreign.getsockname()[1]
        try:
            _write_port(project, busy)
            _write_marker(project, time.time() - 3600)
            _create_todo(project, "TODO-0001")
            _mock_stages(monkeypatch, project)

            from awf.orchestrator import run_pipeline
            assert run_pipeline(_make_args(project)) == 0

            assert _read_port(project) != busy, "the fallback landed on the busy port"
            ts = float(_read_marker(project)["ts"])
            assert ts > time.time() - 300, (
                "fallback to a new port did not refresh the marker — the "
                "plugin would treat the fresh server as a reuse and never open"
            )
        finally:
            foreign.close()


class TestDashboardOpenReuse:
    """Invariants 2-3 (plugin side): _open_dashboard_sync with wait=True
    (just launched) opens a tab only on a fresh boot marker; on a reused
    server it returns the URL without calling webbrowser.open. The
    explicit path (wait=False) always opens."""

    @pytest.fixture
    def live_port(self, project, monkeypatch):
        """A real listener on loopback — _port_alive is a plain TCP
        connect, no HTTP needed here."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        yield port
        sock.close()

    @pytest.fixture
    def port_file(self, project, live_port):
        _write_port(project, live_port)
        return live_port

    def test_fresh_marker_opens(self, project, port_file, monkeypatch):
        """(a) the marker belongs to THIS launch (ts at/after the launch
        start) — any old tab pointed at a dead port: open as before."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        now = time.time()
        _write_marker(project, now)

        result = awf._open_dashboard_sync(project, wait=True, launched_at=now)

        assert result["opened"] is True
        assert result["method"] == "http"
        assert result["url"] == f"http://127.0.0.1:{port_file}"
        assert opened == [result["url"]]

    def test_stale_marker_reuse_skips_open(self, project, port_file, monkeypatch):
        """(b) the marker belongs to a PREVIOUS launch — the port was
        reused, the old tab still shows this run: no second tab, the URL
        and the 'already running' text come back instead."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        now = time.time()
        _write_marker(project, now - 3600)

        result = awf._open_dashboard_sync(project, wait=True, launched_at=now)

        assert result["opened"] is False, (
            "webbrowser.open path taken on a reused server — the owner's "
            "repro: a second tab on top of the live one"
        )
        assert result.get("reused") is True
        assert result["url"] == f"http://127.0.0.1:{port_file}"
        assert (
            f"Dashboard already running at http://127.0.0.1:{port_file}"
            in result["next_action"]
        )
        assert opened == []

    def test_no_marker_opens_conservatively(self, project, port_file, monkeypatch):
        """(c) no marker at all (first launch on an old engine, deleted
        state) — opening is the only safe move."""
        assert not _marker_file(project).exists()
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        result = awf._open_dashboard_sync(project, wait=True, launched_at=time.time())

        assert result["opened"] is True
        assert opened == [result["url"]]

    def test_corrupt_marker_opens_conservatively(self, project, port_file, monkeypatch):
        """(c) a garbage marker is corruption, not a reuse signal — open
        (a broken file must never refuse the dashboard)."""
        f = _marker_file(project)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("not-json", encoding="utf-8")
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))

        result = awf._open_dashboard_sync(project, wait=True, launched_at=time.time())

        assert result["opened"] is True
        assert opened == [result["url"]]

    def test_explicit_open_ignores_stale_marker(self, project, port_file, monkeypatch):
        """(d) the explicit awf_open_pipeline_dashboard path (wait=False)
        always opens — even right after a reuse launch."""
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_marker(project, time.time() - 3600)

        result = awf._open_dashboard_sync(project, wait=False)

        assert result["opened"] is True
        assert result["method"] == "http"
        assert opened == [result["url"]]

    def test_start_next_action_names_running_dashboard(self, project, port_file, monkeypatch):
        """Invariant 4 (text = truth): a reuse launch must not claim the
        dashboard was opened — the next_action names the running URL."""
        class _R:
            def as_dict(self):
                return {"run_mode": "background", "run_id": 1,
                        "log_file": "x.log", "message": "started"}

        monkeypatch.setattr(api, "start_pipeline", lambda *a, **kw: _R())
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_marker(project, time.time() - 3600)

        result = asyncio.run(awf.awf_start(project_dir=str(project), background=True))

        assert result["status"] == "ok"
        assert result["dashboard_opened"] is False
        assert result.get("dashboard_url") == f"http://127.0.0.1:{port_file}"
        assert (
            f"Dashboard already running at http://127.0.0.1:{port_file}"
            in result["next_action"]
        )
        assert opened == []

    def test_continue_next_action_names_running_dashboard(self, project, port_file, monkeypatch):
        """Invariant 4 for awf_continue: a reuse launch must not claim an
        opened dashboard — the next_action names the running URL."""
        class _R:
            def as_dict(self):
                return {"run_mode": "background", "run_id": 1,
                        "log_file": "x.log", "message": "resumed"}

        monkeypatch.setattr(api, "continue_pipeline", lambda *a, **kw: _R())
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_marker(project, time.time() - 3600)

        result = asyncio.run(awf.awf_continue(project_dir=str(project), background=True))

        assert result["status"] == "ok"
        assert result["dashboard_opened"] is False
        assert result.get("dashboard_url") == f"http://127.0.0.1:{port_file}"
        assert "Pipeline resumed" in result["next_action"]
        assert (
            f"Dashboard already running at http://127.0.0.1:{port_file}"
            in result["next_action"]
        )
        assert "no new tab was created" in result["next_action"]
        assert opened == []

    def test_run_next_next_action_names_running_dashboard(self, project, port_file, monkeypatch):
        """Invariant 4 for awf_run_next: the run-loop instruction stays,
        the reuse fact is appended (not replaced)."""
        class _R:
            def as_dict(self):
                return {
                    "action": "started",
                    "todo_id": "TODO-0001",
                    "run_mode": "background",
                    "run_id": 1,
                    "log_file": "x.log",
                    "next_action": "Run item 1/1 launched (TODO-0001). Continue the run loop.",
                }

        monkeypatch.setattr(api, "run_next", lambda *a, **kw: _R())
        opened: list[str] = []
        monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
        _write_marker(project, time.time() - 3600)

        result = asyncio.run(awf.awf_run_next(project_dir=str(project)))

        assert result["status"] == "ok"
        assert result["dashboard_opened"] is False
        assert result.get("dashboard_url") == f"http://127.0.0.1:{port_file}"
        assert "Continue the run loop" in result["next_action"]
        assert (
            f"Dashboard already running at http://127.0.0.1:{port_file}"
            in result["next_action"]
        )
        assert "no new tab was created" in result["next_action"]
        assert opened == []
