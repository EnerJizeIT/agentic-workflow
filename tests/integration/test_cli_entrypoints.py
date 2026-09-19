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


class TestApproveEntrypoint:
    def test_approve_creates_signal(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliApprove")
        capsys.readouterr()

        rc = cli.main(["approve", "TODO-0001", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").is_file()

    def test_approve_invalid_id_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliApprove")
        capsys.readouterr()

        rc = cli.main(["approve", "not-a-todo", "--project-dir", str(repo)])

        assert rc == 1


class TestRollbackEntrypoint:
    def test_dry_run_after_baseline(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRollback")
        api.create_baseline(repo, "TODO-0001")
        capsys.readouterr()

        rc = cli.main(["rollback", "TODO-0001", "--dry-run", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Rolling back" in out or "baseline" in out

    def test_missing_baseline_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRollback")
        capsys.readouterr()

        rc = cli.main(["rollback", "TODO-0001", "--dry-run", "--project-dir", str(repo)])

        assert rc == 1


class TestResetEntrypoint:
    def test_tasks_only_reset(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliReset")
        (repo / ".agentic" / "inbox" / "TODO-0001.md").write_text("# T\n", encoding="utf-8")
        capsys.readouterr()

        rc = cli.main(["reset", "--tasks-only", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Reset complete" in out or "reset" in out.lower()

    def test_orphans_none_is_graceful(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliReset")
        capsys.readouterr()

        rc = cli.main(["reset", "--orphans", "--force", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0
        assert "No orphans" in out or "orphan" in out.lower()


class TestAnalyzeRolesEntrypoint:
    def test_no_roles_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAnalyze")
        capsys.readouterr()

        rc = cli.main(["analyze-roles", "--dry-run", "--project-dir", str(repo)])

        assert rc == 1

    def test_with_roles_reports(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAnalyze")
        roles = repo / ".agentic" / "roles"
        roles.mkdir(parents=True, exist_ok=True)
        (roles / "developer.md").write_text("# Developer\n", encoding="utf-8")
        (roles / "tester.md").write_text("# Tester\n", encoding="utf-8")
        pipes = repo / ".agentic" / "pipelines"
        pipes.mkdir(parents=True, exist_ok=True)
        (pipes / "default.yaml").write_text(
            "stages:\n"
            "  - name: plan\n    role: supervisor\n    kind: plan\n"
            "  - name: developer\n    role: developer\n    kind: execute\n"
            "  - name: tester\n    role: tester\n    kind: execute\n"
            "  - name: verify\n    role: supervisor\n    kind: verify\n",
            encoding="utf-8",
        )
        capsys.readouterr()

        rc = cli.main(["analyze-roles", "--dry-run", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "ROLE ANALYSIS" in out or "developer" in out


class TestInitDryRunCli:
    """AUD07-07: dry-run is a pure read — no prompts, no writes, honest message."""

    def test_non_interactive_dry_run_preserves_runtime(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="DryRun")
        (repo / ".agentic" / "context" / "marker.md").write_text(
            "marker\n", encoding="utf-8"
        )
        capsys.readouterr()

        rc = cli.main(["init", "--dry-run", "--non-interactive", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "context" / "marker.md").is_file(), (
            "dry-run deleted runtime data"
        )
        assert "No files written" in out

    def test_interactive_dry_run_asks_no_prompts(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)

        def fail_input(prompt=""):
            raise AssertionError(f"dry-run must not prompt, but asked: {prompt!r}")

        monkeypatch.setattr("builtins.input", fail_input)
        capsys.readouterr()

        rc = cli.main(["init", "--dry-run", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "DRY RUN" in out
        assert not (repo / ".agentic").exists(), "dry-run must not create .agentic/"

    def test_blank_project_name_falls_back_to_dir_name(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        # name=blank, 4 commands=blank, then decline any follow-up offers
        answers = iter(["", "", "", "", "", "n", "n", "n"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
        monkeypatch.setattr(
            "awf.cmd_init.opencode_config_file",
            lambda: tmp_path / "nope" / "opencode.json",
        )
        capsys.readouterr()

        rc = cli.main(["init", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        cfg_text = (repo / ".agentic" / "config.yaml").read_text(encoding="utf-8")
        assert "name: ''" not in cfg_text, "blank prompt answer leaked into config"
        assert "name: 'Repo'" in cfg_text, "expected name derived from dir name 'repo'"
