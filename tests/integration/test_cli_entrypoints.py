"""NEG-2026-09-19 · CLI entry points: real runs, no mocks.

The worst coverage holes were exactly the entry points an owner touches
first (cmd_init 0%, cmd_status 10%, dashboard_server 36%). These tests run
the real code paths against real temp projects.
"""
from __future__ import annotations

import json
import subprocess
import urllib.request
from pathlib import Path

from awf import api, cli


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


class TestInitEntrypoint:
    def test_init_non_interactive_creates_project(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)

        rc = cli.main(["init", "--non-interactive", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "config.yaml").is_file()
        assert (repo / ".agentic" / "inbox").is_dir()

    def test_init_twice_without_force_is_safe(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        cli.main(["init", "--non-interactive", "--project-dir", str(repo)])
        capsys.readouterr()

        rc = cli.main(["init", "--non-interactive", "--project-dir", str(repo)])

        assert rc == 0
        assert (repo / ".agentic" / "config.yaml").is_file()


class TestStatusEntrypoint:
    def test_status_on_initialized_project(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliStatus")
        capsys.readouterr()

        rc = cli.main(["status", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "CliStatus" in out or "status" in out.lower()


class TestContinueEntrypointNoop:
    def test_continue_without_todo_is_graceful(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliContinue")
        capsys.readouterr()

        rc = cli.main(["continue", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "No active TODO" in out or "no active todo" in out.lower()


class TestDashboardServerReal:
    """The HTTP server is what the owner actually looks at — test it live."""

    def test_state_endpoint_and_404(self, tmp_path):
        from awf.api.dashboard_server import start_dashboard_server

        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="DashSrv")

        port, server = start_dashboard_server(repo)
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state", timeout=5,
            ) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["project_name"] == "DashSrv"
                assert "status" in data

            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
                raise AssertionError("unknown path must 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404

            # HTML before generation → 404; after generate_dashboard → 200
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5)
                raise AssertionError("dashboard not generated yet → 404")
            except urllib.error.HTTPError as e:
                assert e.code == 404
            from awf.api.dashboard import generate_dashboard

            generate_dashboard(repo)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
                assert resp.status == 200
                assert "<html" in resp.read().decode("utf-8").lower()
        finally:
            server.shutdown()
            server.server_close()
