"""REPORTS29: the dashboard must keep ONE port across consecutive launches.

The owner's day (08.10): 33833 → 43785 → 43809 ... — every
start/continue opened a new browser tab on a new port. The reuse
mechanism formally existed (state/dashboard_port + bind attempt), but
the port file was unlinked in the orchestrator's finally on EVERY exit,
so the second launch never saw the first's port and bound a fresh
random one.

Invariant (TODO-0174):
1. Consecutive launches (start/continue/run_next) in ONE project get
   the SAME port while it is free/reusable — a dead own server leaves
   TIME_WAIT, which SO_REUSEADDR (HTTPServer default) rebinds cleanly.
2. A port held by a FOREIGN process → honest fallback to a new port,
   and the file is rewritten with it.
3. A killed run (SIGKILL, no finally) leaves the file behind — the
   next launch must rebind that port, not treat the leftover as poison.
"""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api


def _port_file(project: Path) -> Path:
    return project / ".agentic" / "state" / "dashboard_port"


def _read_port(project: Path) -> int:
    return int(_port_file(project).read_text(encoding="utf-8").strip())


def _write_port(project: Path, port: int) -> None:
    f = _port_file(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(port), encoding="utf-8")


@pytest.fixture
def project(tmp_git_repo):
    """Project with git + .agentic/ + plan→worker→verify pipeline."""
    repo = tmp_git_repo
    api.init_project(repo, project_name="PortReuse")

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


class TestDashboardPortReuse:
    def test_two_consecutive_launches_get_same_port(self, project, monkeypatch):
        """The owner's repro: start → complete → start again, ONE project.

        Baseline bug: run_pipeline unlinked state/dashboard_port in its
        finally, so launch #2 started with no previous port and bound a
        fresh random one. The file must survive the exit and launch #2
        must rebind it.
        """
        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        rc1 = run_pipeline(_make_args(project))
        assert rc1 == 0

        assert _port_file(project).is_file(), (
            "port file gone after a clean exit — the next launch has no "
            "port to reuse (baseline: the orchestrator unlinks it in finally)"
        )
        port1 = _read_port(project)
        assert port1 > 0

        _create_todo(project, "TODO-0002")
        rc2 = run_pipeline(_make_args(project))
        assert rc2 == 0

        assert _port_file(project).is_file(), "port file gone after the second exit"
        port2 = _read_port(project)
        assert port2 == port1, f"port changed between launches: {port1} -> {port2}"

    def test_busy_port_falls_back_and_updates_file(self, project, monkeypatch):
        """Port held by a FOREIGN process: honest fallback to a new port,
        and the file is rewritten so the NEXT launch reuses the new one."""
        foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen(1)
        busy = foreign.getsockname()[1]
        try:
            _write_port(project, busy)
            _create_todo(project, "TODO-0001")
            _mock_stages(monkeypatch, project)

            from awf.orchestrator import run_pipeline
            rc = run_pipeline(_make_args(project))
            assert rc == 0

            assert _port_file(project).is_file(), (
                "port file gone after exit — the fallback port would be "
                "lost for the next launch"
            )
            port = _read_port(project)
            assert port != busy, "the fallback landed on the foreign-occupied port"
            assert port > 0
        finally:
            foreign.close()

    def test_leftover_file_after_dead_server_rebinds_same_port(self, project, monkeypatch):
        """Invariant 4: a killed run (SIGKILL) never runs the orchestrator's
        finally — the port file survives with the dead server's port. The
        next launch must rebind that exact port: the leftover file is a
        reuse token, not poison. A dead own socket leaves TIME_WAIT, which
        SO_REUSEADDR rebinds cleanly."""
        from awf.api.dashboard_server import start_dashboard_server

        port, server = start_dashboard_server(project)
        assert port > 0
        server.shutdown()
        server.server_close()  # the "killed" server — dead, its file stays
        _write_port(project, port)

        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        rc = run_pipeline(_make_args(project))
        assert rc == 0

        assert _port_file(project).is_file(), (
            "the leftover port file was destroyed on exit — a killed run "
            "would leave no reuse token at all"
        )
        assert _read_port(project) == port, (
            "the dead server's port was not rebound — the leftover file was "
            "treated as poison (random port) instead of a reuse token"
        )

    def test_out_of_range_port_file_heals_not_poisons(self, project, monkeypatch):
        """A port file holding an out-of-range number (99999) is corruption,
        not a reuse token. _read_reuse_port promises 0 for out-of-range
        values, and the launch must rebind a fresh valid port and rewrite
        the file. As shipped, bind() raises OverflowError (not OSError),
        the except-OSError fallback is skipped, run_pipeline's generic
        except swallows it, and the file keeps the poison value: EVERY
        future launch loses the dashboard until someone edits the file."""
        _write_port(project, 99999)
        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        rc = run_pipeline(_make_args(project))
        assert rc == 0

        assert _port_file(project).is_file(), "port file destroyed on exit"
        port = _read_port(project)
        assert 0 < port <= 65535, (
            f"out-of-range port file not healed (still {port!r}) — the "
            "dashboard is lost for every future launch (invariant 4)"
        )

    def test_garbage_port_file_heals_not_poisons(self, project, monkeypatch):
        """A port file holding non-numeric garbage is corruption, not a
        reuse token: _read_reuse_port promises 0 for it, the launch must
        rebind a fresh valid port and rewrite the file."""
        _write_port(project, "abc")
        _create_todo(project, "TODO-0001")
        _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        rc = run_pipeline(_make_args(project))
        assert rc == 0

        raw = _port_file(project).read_text(encoding="utf-8").strip()
        assert raw.isdigit() and 0 < int(raw) <= 65535, (
            f"garbage port file not healed (still {raw!r}) — the "
            "dashboard is lost for every future launch (invariant 4)"
        )
