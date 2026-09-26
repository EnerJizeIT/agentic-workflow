"""NEG-2026-09-19 · CLI entry points: real runs, no mocks.

The worst coverage holes were exactly the entry points an owner touches
first (cmd_init 0%, cmd_status 10%, dashboard_server 36%). These tests run
the real code paths against real temp projects.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from conftest import _git_init  # AUD12-08: shared git boilerplate

from awf import api, cli
from awf.pipeline_state import write_state


def _git_repo(tmp_path: Path) -> Path:
    """Git repo with an initial commit (AUD12-08: boilerplate in conftest)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
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

    def test_continue_ack_garbage_returns_1(self, tmp_path, capsys):
        """AUD07-01: a user-caused refusal (garbage --ack) is non-zero rc.

        State-condition noops (no active TODO, pipeline already running)
        stay rc 0; a typo in --ack is an owner mistake `set -e` must catch.
        """
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAck")
        capsys.readouterr()

        rc = cli.main(["continue", "--ack", "garbage", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Invalid TODO id" in out


class TestStartRcSemantics:
    """AUD07-01: any StartResult with exit_code != 0 reaches the user as rc != 0.

    The CLI used to swallow exit_code on run_mode="noop" and always return 0,
    so `awf start && next_step` sailed through a refusal (e.g. the
    foreground+checkpoint incompatibility).
    """

    def test_foreground_start_checkpoint_conflict_returns_1(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliStartRc")
        # conftest sets AWF_PLAN_CHECKPOINT=false globally; remove it so the
        # checkpoint is enabled → foreground start is incompatible → exit 1.
        # Also clear AWF_BACKGROUND_CHILD — the DF6-5 except-branch reads it,
        # and a leaked value (AUD04-10 class) would skip the guard entirely.
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.delenv("AWF_BACKGROUND_CHILD", raising=False)
        capsys.readouterr()

        rc = cli.main(["start", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "incompatible" in out.lower() or "checkpoint" in out.lower()


class TestContinueBackgroundFlag:
    """AUD07-02: `awf continue` gets --background (parity with start).

    Default is foreground (a human in the terminal blocks until the pipeline
    finishes); --background detaches. The old code always detached and the
    help claimed the flag was "start only".
    """

    def test_continue_with_background_flag_detaches(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliContBg")
        inbox = repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# Task")
        (inbox / "TODO-0001.ready").touch()
        capsys.readouterr()

        class _FakeProc:
            def __init__(self, args_list, **kwargs):
                self.pid = 12345

        with patch.object(api._background.subprocess, "Popen", _FakeProc), \
             patch("awf.api.pipeline._verify_child_alive", return_value=True):
            rc = cli.main(["continue", "--background", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "running in background" in out

    def test_continue_default_is_foreground(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliContFg")
        inbox = repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text("# Task")
        (inbox / "TODO-0001.ready").touch()
        capsys.readouterr()

        spawned: dict = {}

        class _PopenMustNotBeCalled:
            def __init__(self, *a, **k):
                spawned["called"] = True

        import awf.orchestrator as orch_mod

        monkeypatch.setattr(orch_mod, "run_pipeline", lambda args: 0)
        with patch.object(api._background.subprocess, "Popen", _PopenMustNotBeCalled):
            rc = cli.main(["continue", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert not spawned.get("called"), (
            "continue without --background must not spawn a detached subprocess"
        )


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
            "  - name: plan\n    role: supervisor\n"
            "  - name: developer\n    role: developer\n"
            "  - name: tester\n    role: tester\n"
            "  - name: verify\n    role: supervisor\n",
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

    def test_interactive_dry_run_existing_project_honest_message(
        self, tmp_path, monkeypatch, capsys
    ):
        """FU-19 (backlog tail): with .agentic/ present the dry-run must not
        print 'Would create:' — it would lie about the R1 branch."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="DryRunExisting")
        capsys.readouterr()

        rc = cli.main(["init", "--dry-run", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Would create" not in out, "dry-run lied about creating an existing project"
        assert "already exists" in out
        assert "No files written" in out


class TestAddRoleEntrypoint:
    """AUD12-07: cmd_add_role CLI smoke.

    The wrapper was at 65% — the API (api.add_role) was well covered, the
    CLI entry point (flag plumbing, error → rc) was not.
    """

    def test_add_role_with_model_flag(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAddRole")
        capsys.readouterr()

        rc = cli.main(["add-role", "qa", "--model", "vllm/llm", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        role_file = repo / ".agentic" / "roles" / "qa.md"
        assert role_file.is_file()
        assert "vllm/llm" in role_file.read_text(encoding="utf-8")

    def test_add_role_traversal_name_rejected_no_traceback(self, tmp_path, capsys):
        """AUD06-06 CLI surface: '../../evil' must not escape .agentic/roles/."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAddRoleTrav")
        capsys.readouterr()

        rc = cli.main(["add-role", "../../evil", "--model", "m", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "Traceback" not in captured.out + captured.err
        assert not (repo / "evil.md").exists()
        assert not (tmp_path / "evil.md").exists()

    def _project_skill(self, repo: Path, name: str = "demo-skill") -> None:
        skill_dir = repo / ".opencode" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: demo-skill\n---\n\n# Demo Skill\n\nCLI demo body line.\n",
            encoding="utf-8",
        )

    def test_add_role_from_skill_flag(self, tmp_path, capsys):
        """RUN3 #3: --from-skill copies the skill body, no model prompt."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAddRoleSkill")
        self._project_skill(repo)
        capsys.readouterr()

        rc = cli.main(["add-role", "demo-role", "--from-skill", "demo-skill",
                       "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "content copied from skill" in out
        role_file = repo / ".agentic" / "roles" / "demo-role.md"
        assert role_file.is_file()
        content = role_file.read_text(encoding="utf-8")
        assert "CLI demo body line." in content
        assert not content.startswith("---")

    def test_add_role_from_skill_unknown_no_traceback(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliAddRoleSkillMiss")
        self._project_skill(repo)
        capsys.readouterr()

        rc = cli.main(["add-role", "x", "--from-skill", "ghost",
                       "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "Traceback" not in captured.out + captured.err
        assert "ghost" in captured.out
        assert "demo-skill" in captured.out  # available skills listed
        assert not (repo / ".agentic" / "roles" / "x.md").exists()


class TestReportEntrypoint:
    """AUD12-07: cmd_report CLI smoke (wrapper was at 68%)."""

    def test_report_on_live_project(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliReport")
        inbox = repo / ".agentic" / "inbox"
        outbox = repo / ".agentic" / "outbox"
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)
        # TODO-0001: DONE signal → OK; TODO-0002: BLOCKED signal → BLK
        for tid, sig in (("TODO-0001", "DONE"), ("TODO-0002", "BLOCKED")):
            (inbox / f"{tid}.md").write_text(f"# {tid}\n", encoding="utf-8")
            (inbox / f"{tid}.ready").touch()
            (outbox / f"{sig}-{tid}.ready").touch()
        capsys.readouterr()

        rc = cli.main(["report", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "CliReport" in out
        assert "OK   TODO-0001" in out
        assert "BLK  TODO-0002" in out
        assert "Blocked: 1" in out

    def test_report_missing_agentic_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        capsys.readouterr()

        rc = cli.main(["report", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Traceback" not in out
        assert ".agentic" in out


class TestKillEntrypoint:
    """RUN9 #1: ``awf kill`` — the CLI twin of the MCP ``awf_kill`` tool.

    Recovery without MCP: with the MCP server down a stuck pipeline had to
    be killed from the terminal; the command calls the same
    ``api.kill_pipeline`` the MCP tool uses (RUN8 #2: pipeline + worker).
    """

    def test_kill_without_agentic_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        capsys.readouterr()

        rc = cli.main(["kill", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Traceback" not in out
        assert ".agentic" in out

    def test_kill_no_pipeline_returns_0(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliKill")
        capsys.readouterr()

        rc = cli.main(["kill", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "No running pipeline" in out

    def test_kill_kills_live_dummy_pipeline(self, tmp_path, capsys):
        """A live dummy with a ``python -m awf start`` argv: the real
        liveness identity check must accept it (QA .14 — a bare
        ``sleep 300`` would be refused as a recycled PID)."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliKillLive")

        # A fake ``awf`` package: from its directory ``python -m awf start``
        # is a real living process whose /proc cmdline is exactly what
        # _liveness.resolve looks for (``-m awf start|continue``).
        fake = tmp_path / "fake_awf"
        (fake / "awf").mkdir(parents=True)
        (fake / "awf" / "__main__.py").write_text(
            "import time\ntime.sleep(300)\n", encoding="utf-8"
        )
        proc = subprocess.Popen(
            [sys.executable, "-m", "awf", "start", "--project-dir", "/x"],
            cwd=str(fake),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            write_state(repo, pipeline_pid=proc.pid)
            capsys.readouterr()

            rc = cli.main(["kill", "--project-dir", str(repo)])

            out = capsys.readouterr().out
            assert rc == 0, out
            assert str(proc.pid) in out
            assert "killed" in out.lower()

            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.2)
            assert proc.poll() is not None, "dummy pipeline survived awf kill"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

    def test_kill_failure_returns_1(self, tmp_path, capsys):
        """An explicit kill failure is an error (rc=1), not a silent no-op.

        No process involved: the API result is pinned, the contract under
        test is the CLI's rc mapping (Part A.1 of the unit: «Failed to
        kill» → rc=1)."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliKillFail")
        capsys.readouterr()

        with patch(
            "awf.cmd_kill.api.kill_pipeline",
            return_value={
                "killed": False,
                "pid": 4242,
                "workers": {},
                "message": "Failed to kill PID 4242.",
            },
        ):
            rc = cli.main(["kill", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "Failed to kill" in out


class TestApproveEvidenceFlag:
    """AUD07-04: `awf approve --evidence` (parity with the MCP tool).

    In run (забег) mode the API REQUIRES evidence; without the flag the
    CLI command was unusable during a run.
    """

    def _active_run(self, repo: Path) -> None:
        from awf import run_state

        run_state.write_run(
            repo,
            active=True,
            queue=["TODO-0001"],
            index=0,
            current="TODO-0001",
        )

    def test_approve_with_evidence_in_run_mode(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliApproveEv")
        self._active_run(repo)
        capsys.readouterr()

        rc = cli.main(
            [
                "approve", "TODO-0001",
                "--evidence", "pytest -q → 348 passed; verdict: approve",
                "--project-dir", str(repo),
            ]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").is_file()
        assert (repo / ".agentic" / "context" / "RUN-EVIDENCE-TODO-0001.md").is_file()

    def test_approve_without_evidence_in_run_mode_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliApproveNoEv")
        self._active_run(repo)
        capsys.readouterr()

        rc = cli.main(["approve", "TODO-0001", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "evidence" in out.lower()
        assert "Traceback" not in out


class TestTopLevelHelp:
    """AUD07-08: `awf help` lists every registered subcommand.

    The old hand-maintained list drifted (restore was in the parser but
    missing from the help). The list is now generated from the live
    subparsers — this test pins the invariant.
    """

    def test_help_lists_all_subcommands(self, capsys):
        rc = cli.main(["help"])

        out = capsys.readouterr().out
        assert rc == 0
        _parser, sub = cli._build_parser()
        missing = [
            name
            for name in sub.choices
            if not re.search(rf"^\s+{re.escape(name)}\s", out, flags=re.M)
        ]
        assert not missing, f"subcommands missing from `awf help`: {missing}"

    def test_unknown_command_is_nonzero(self):
        """A command unknown to argparse exits 2 — never a silent 0."""
        with pytest.raises(SystemExit) as exc:
            cli.main(["definitely-not-a-command"])
        assert exc.value.code == 2


class TestResetHelpMatchesBehavior:
    """AUD07-03: the reset help text must name what reset actually cleans.

    The old help promised a gentle "inbox/outbox/logs" clean while the
    default wiped context/state too. --full is now behaviorally
    different (also handoff/inputs/dashboards).
    """

    def _plant_junk(self, repo: Path, dirs) -> None:
        ag = repo / ".agentic"
        for d in dirs:
            (ag / d).mkdir(parents=True, exist_ok=True)
            (ag / d / "junk.txt").write_text("junk", encoding="utf-8")

    def test_full_mode_cleans_more_than_default(self, tmp_path):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliResetDiff")
        self._plant_junk(
            repo, ["inbox", "outbox", "context", "logs",
                   "handoff", "inputs", "dashboards"]
        )
        full = api.reset_runtime(repo, full=True)
        assert {"handoff", "inputs", "dashboards"} <= set(full.cleaned_dirs)

        self._plant_junk(
            repo, ["inbox", "outbox", "context", "logs",
                   "handoff", "inputs", "dashboards"]
        )
        default = api.reset_runtime(repo)
        assert {"handoff", "inputs", "dashboards"} & set(default.cleaned_dirs) == set(), (
            "default reset must keep handoff/inputs/dashboards (that is what "
            f"--full is for); got {default.cleaned_dirs}"
        )
        assert {"inbox", "outbox", "context", "logs"} <= set(default.cleaned_dirs)

    def test_reset_help_names_the_cleaned_dirs(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliResetHelp")
        capsys.readouterr()

        with pytest.raises(SystemExit):
            cli.main(["reset", "--help"])
        help_out = capsys.readouterr().out

        for d in ("inbox", "outbox", "context", "logs",
                  "handoff", "inputs", "dashboards"):
            assert d in help_out, f"reset --help does not name cleaned dir {d!r}"


class TestTreeShaEntrypoint:
    """U11: `awf tree-sha` — the supervisor's one-liner for verified-sha."""

    def test_prints_stable_hex64(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        capsys.readouterr()

        rc1 = cli.main(["tree-sha", "--project-dir", str(repo)])
        out1 = capsys.readouterr().out
        rc2 = cli.main(["tree-sha", "--project-dir", str(repo)])
        out2 = capsys.readouterr().out

        assert rc1 == 0 and rc2 == 0
        assert len(out1.strip()) == 64
        assert out1.strip() == out2.strip()

    def test_changes_after_edit(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        capsys.readouterr()
        rc = cli.main(["tree-sha", "--project-dir", str(repo)])
        fp1 = capsys.readouterr().out.strip()
        (repo / "README.md").write_text("tampered\n")
        rc = cli.main(["tree-sha", "--project-dir", str(repo)])
        fp2 = capsys.readouterr().out.strip()
        assert rc == 0
        assert fp1 != fp2


class TestApproveVerifiedShaCli:
    """U11: `awf approve --verified-sha` (parity with the MCP tool)."""

    TID = "TODO-0001"

    def test_matching_sha_ok(self, tmp_path, capsys):
        from awf import git_utils

        repo = _git_repo(tmp_path)
        (repo / ".agentic").mkdir()
        fp = git_utils.tree_fingerprint(repo)
        capsys.readouterr()

        rc = cli.main([
            "approve", self.TID, "--verified-sha", fp,
            "--project-dir", str(repo),
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "inbox" / f"APPROVE-{self.TID}.ready").is_file()
        verified = repo / ".agentic" / "context" / f"VERIFIED-{self.TID}.sha"
        assert verified.is_file()
        assert verified.read_text(encoding="utf-8").strip() == fp

    def test_mismatch_returns_1_with_text(self, tmp_path, capsys):
        from awf import git_utils

        repo = _git_repo(tmp_path)
        (repo / ".agentic").mkdir()
        fp = git_utils.tree_fingerprint(repo)
        (repo / "README.md").write_text("moved after verify\n")
        capsys.readouterr()

        rc = cli.main([
            "approve", self.TID, "--verified-sha", fp,
            "--project-dir", str(repo),
        ])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "tree changed after verification" in captured.out + captured.err
        assert not (repo / ".agentic" / "inbox" / f"APPROVE-{self.TID}.ready").is_file()


class TestMutationsEntrypoint:
    """U11/B6: `awf mutations` CLI surface (list, refusal, real small run)."""

    _MUTATIONS = (
        "target.py @@ x = 1 @@ x = 2 @@ false\n"
        "target.py @@ x = 1 @@ x = 1 @@ true\n"
    )

    def _repo_with_mutations(self, tmp_path) -> Path:
        repo = _git_repo(tmp_path)
        (repo / "target.py").write_text("x = 1\n")
        scripts = repo / "scripts"
        scripts.mkdir()
        (scripts / "mutations.txt").write_text(self._MUTATIONS, encoding="utf-8")
        from subprocess import run as _run

        _run(["git", "add", "-A"], cwd=repo, check=True)
        _run(["git", "commit", "-qm", "add mutations"], cwd=repo, check=True)
        return repo

    def test_list_prints_mutations(self, tmp_path, capsys):
        repo = self._repo_with_mutations(tmp_path)
        capsys.readouterr()

        rc = cli.main(["mutations", "--list", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert "target.py" in out
        assert "x = 1" in out
        assert "2" in out  # total line

    def test_dirty_tree_refused(self, tmp_path, capsys):
        repo = self._repo_with_mutations(tmp_path)
        (repo / "target.py").write_text("x = 999\n")  # uncommitted
        capsys.readouterr()

        rc = cli.main(["mutations", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "dirty" in captured.out + captured.err

    def test_not_a_repo_refused(self, tmp_path, capsys):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "m.txt").write_text("a.py @@ one @@ two @@ true\n")
        capsys.readouterr()

        rc = cli.main(["mutations", "--file", "m.txt", "--project-dir", str(plain)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "git repo" in captured.out + captured.err

    def test_real_run_reports_killed_and_survived(self, tmp_path, capsys):
        """`false`-command mutation is killed (tests went red), the
        no-op mutation (`x = 1` → `x = 1`) survives: rc must be 1."""
        repo = self._repo_with_mutations(tmp_path)
        capsys.readouterr()

        rc = cli.main(["mutations", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 1, out
        assert "killed" in out
        assert "survived" in out
        # the tree must be back to the committed state
        from subprocess import run as _run

        diff = _run(
            ["git", "diff", "--quiet"], cwd=repo, capture_output=True, check=False
        )
        assert diff.returncode == 0, "mutation run left the tree modified"


class TestTodoDraftEntrypoint:
    """U11/E3: `awf todo-draft` CLI surface."""

    _INDEX = (
        "| ID | Sev | Тип | Заголовок | Итерация | Фикс | Статус |\n"
        "|----|-----|-----|-----------|----------|------|--------|\n"
        "| DEMO-01 | P1 | BUG | порча run.yaml (awf/run_state.py:55) | 02 | S | ✅ FU-13 (abc1234) |\n"
        "\n"
        "| Юнит | Тема | ID (суммарно) | Фикс | Волна |\n"
        "|------|------|---------------|------|-------|\n"
        "| FU-13 | Гигиена state | 1 | M | 3 |\n"
    )

    def _repo_with_index(self, tmp_path) -> Path:
        repo = _git_repo(tmp_path)
        (repo / "AUDIT-INDEX.md").write_text(self._INDEX, encoding="utf-8")
        return repo

    def test_out_writes_skeleton(self, tmp_path, capsys):
        repo = self._repo_with_index(tmp_path)
        out = tmp_path / "draft.md"
        capsys.readouterr()

        rc = cli.main([
            "todo-draft", "FU-13", "--out", str(out), "--project-dir", str(repo),
        ])

        outtext = capsys.readouterr().out
        assert rc == 0, outtext
        assert out.is_file()
        body = out.read_text(encoding="utf-8")
        assert "FU-13" in body
        assert "DEM" in body or "run_state" in body
        assert "awf/run_state.py" in body

    def test_existing_out_refused_then_force(self, tmp_path, capsys):
        repo = self._repo_with_index(tmp_path)
        out = tmp_path / "draft.md"
        out.write_text("old")
        capsys.readouterr()

        rc = cli.main([
            "todo-draft", "FU-13", "--out", str(out), "--project-dir", str(repo),
        ])
        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "old" == out.read_text(encoding="utf-8")

        rc = cli.main([
            "todo-draft", "FU-13", "--out", str(out), "--force",
            "--project-dir", str(repo),
        ])
        captured = capsys.readouterr()
        assert rc == 0, captured.out
        assert "FU-13" in out.read_text(encoding="utf-8")

    def test_unknown_id_returns_1(self, tmp_path, capsys):
        repo = self._repo_with_index(tmp_path)
        capsys.readouterr()

        rc = cli.main(["todo-draft", "FU-99", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "not found" in captured.out + captured.err or \
            "не найден" in captured.out + captured.err


class TestMetricsEntrypoint:
    def test_no_mirror_flag_is_accepted(self, tmp_path, capsys):
        """U8c: `awf metrics --no-mirror` — the flag exists and the run
        completes. rc is 0 or 1 depending on whether the machine's
        opencode.db has measurable sessions; the report is written either
        way, and an unrecognized flag would SystemExit(2) instead."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliMetrics")
        capsys.readouterr()
        out_file = tmp_path / "report.md"

        rc = cli.main([
            "metrics", "--project-dir", str(repo),
            "--no-mirror", "--out", str(out_file),
        ])

        assert rc in (0, 1)
        assert out_file.is_file()


class TestPipelineWriteEntrypoint:
    """RUN3 #1: `awf pipeline-write` + `awf pipelines` — real runs, no mocks."""

    def test_pipeline_write_creates_file_keeps_config_and_supervisor(
        self, tmp_path, capsys
    ):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliPipeWrite")
        (repo / ".agentic" / "roles" / "agent-implementer.md").write_text("# r\n")
        config_before = (repo / ".agentic" / "config.yaml").read_text(encoding="utf-8")
        sup_before = (repo / ".agentic" / "roles" / "supervisor.md").read_text(
            encoding="utf-8"
        )
        capsys.readouterr()

        rc = cli.main(
            [
                "pipeline-write", "audit-probe",
                "--role", "agent-implementer",
                "--project-dir", str(repo),
            ]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        target = repo / ".agentic" / "pipelines" / "audit-probe.yaml"
        assert target.is_file()
        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        # stages generated like the setup form: plan → roles → verify
        assert [s["role"] for s in data["stages"]] == [
            "supervisor",
            "agent-implementer",
            "supervisor",
        ]
        # contract: config.yaml and supervisor.md byte-identical
        assert (repo / ".agentic" / "config.yaml").read_text(encoding="utf-8") == (
            config_before
        )
        assert (repo / ".agentic" / "roles" / "supervisor.md").read_text(
            encoding="utf-8"
        ) == sup_before

    def test_pipeline_write_refuses_existing_then_force(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliPipeForce")
        (repo / ".agentic" / "roles" / "worker.md").write_text("# w\n")
        capsys.readouterr()

        rc = cli.main(
            ["pipeline-write", "dup", "--role", "worker", "--project-dir", str(repo)]
        )
        assert rc == 0
        capsys.readouterr()

        rc = cli.main(
            ["pipeline-write", "dup", "--role", "worker", "--project-dir", str(repo)]
        )
        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "already exists" in captured.out + captured.err

        rc = cli.main(
            [
                "pipeline-write", "dup", "--role", "worker", "--force",
                "--project-dir", str(repo),
            ]
        )
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "Overwrote" in out

    def test_pipeline_write_traversal_rejected(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliPipeTrav")
        capsys.readouterr()

        rc = cli.main(
            [
                "pipeline-write", "../../evil", "--role", "worker",
                "--project-dir", str(repo),
            ]
        )

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "Invalid pipeline name" in captured.out + captured.err
        assert not (repo / "evil.yaml").exists()
        assert not (tmp_path / "evil.yaml").exists()

    def test_pipelines_lists_and_marks_active(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliPipeList")
        (repo / ".agentic" / "roles" / "worker.md").write_text("# w\n")
        capsys.readouterr()

        rc = cli.main(
            ["pipeline-write", "audit-llm", "--role", "worker", "--project-dir", str(repo)]
        )
        assert rc == 0
        capsys.readouterr()

        # init doesn't create default.yaml — the active name is still
        # reported (as a note), audit-llm is the only file
        rc = cli.main(["pipelines", "--project-dir", str(repo)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "  audit-llm" in out
        assert "default" in out

        # config-declared active pipeline is starred
        config = repo / ".agentic" / "config.yaml"
        config.write_text(
            config.read_text(encoding="utf-8") + "default_pipeline: audit-llm\n"
        )
        rc = cli.main(["pipelines", "--project-dir", str(repo)])
        out = capsys.readouterr().out
        assert rc == 0, out
        assert "* audit-llm  (active)" in out

    def test_start_missing_pipeline_name_errors_cleanly(self, tmp_path, capsys):
        """Part B: an unknown --pipeline name is a clear error with the
        available list — not a silent run of default.yaml."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliPipeStart")
        (repo / ".agentic" / "roles" / "worker.md").write_text("# w\n")
        capsys.readouterr()
        rc = cli.main(
            ["pipeline-write", "audit-llm", "--role", "worker", "--project-dir", str(repo)]
        )
        assert rc == 0
        capsys.readouterr()

        rc = cli.main(
            ["start", "--pipeline", "no-such-pipeline", "--project-dir", str(repo)]
        )

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        text = captured.out + captured.err
        assert "no-such-pipeline" in text
        assert "not found" in text
        # the available pipelines are listed — the next action is obvious
        assert "Available pipelines: audit-llm" in text


class TestUnblockEntrypoint:
    """RUN3 #4: `awf unblock` — real runs, no mocks."""

    def test_unblock_cli_clears_stale_blocked(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUnblock")
        capsys.readouterr()
        inbox = repo / ".agentic" / "inbox"
        outbox = repo / ".agentic" / "outbox"
        (inbox / "TODO-0001.md").write_text("task")
        (inbox / "TODO-0001.ready").touch()
        (outbox / "BLOCKED-TODO-0001.md").write_text("stale reason")
        (outbox / "BLOCKED-TODO-0001.ready").touch()

        rc = cli.main(["unblock", "TODO-0001", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert not (outbox / "BLOCKED-TODO-0001.ready").exists()
        assert not (outbox / "BLOCKED-TODO-0001.md").exists()
        # the re-issued TODO is visible again
        rc2 = cli.main(["status", "--project-dir", str(repo)])
        out2 = capsys.readouterr().out
        assert rc2 == 0
        assert "TODO-0001" in out2

    def test_unblock_cli_without_closures_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUnblock2")
        capsys.readouterr()

        rc = cli.main(["unblock", "TODO-0042", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "nothing to unblock" in captured.out + captured.err


class TestTodoRemoveEntrypoint:
    """RUN3 #5: `awf todo-remove` — real runs, no mocks."""

    def test_todo_remove_cli_removes_never_started(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRemove")
        capsys.readouterr()
        inbox = repo / ".agentic" / "inbox"
        (inbox / "TODO-0003.md").write_text("never started")

        rc = cli.main(["todo-remove", "TODO-0003", "--project-dir", str(repo)])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert not (inbox / "TODO-0003.md").exists()
        traces = list((repo / ".agentic" / "done" / "TODO-0003").glob("removed-*.md"))
        assert len(traces) == 1
        assert "never started" in traces[0].read_text()

    def test_todo_remove_cli_refuses_armed(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRemove2")
        capsys.readouterr()
        inbox = repo / ".agentic" / "inbox"
        (inbox / "TODO-0003.md").write_text("armed")
        (inbox / "TODO-0003.ready").touch()

        rc = cli.main(["todo-remove", "TODO-0003", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert (inbox / "TODO-0003.md").exists()

    def test_todo_remove_cli_missing_is_error(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRemove3")
        capsys.readouterr()

        rc = cli.main(["todo-remove", "TODO-0077", "--project-dir", str(repo)])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "not found" in captured.out + captured.err


class TestTodoRetireEntrypoint:
    """RUN5 #2: `awf todo-retire` — real runs, no mocks."""

    def _ghost(self, repo: Path) -> None:
        inbox = repo / ".agentic" / "inbox"
        outbox = repo / ".agentic" / "outbox"
        (inbox / "TODO-0001.md").write_text("rejected task")
        (inbox / "TODO-0001.ready").touch()
        (outbox / "DONE-TODO-0001.md").write_text("worker claim")
        (outbox / "REVIEW-TODO-0001.md").write_text("rejected")

    def test_retire_cli_archives_ghost(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRetire")
        self._ghost(repo)
        capsys.readouterr()

        rc = cli.main(
            ["todo-retire", "TODO-0001", "--reason", "rejected at verify",
             "--project-dir", str(repo)]
        )

        out = capsys.readouterr().out
        assert rc == 0, out
        inbox = repo / ".agentic" / "inbox"
        assert not (inbox / "TODO-0001.md").exists()
        assert not (inbox / "TODO-0001.ready").exists()
        done_dir = repo / ".agentic" / "done" / "TODO-0001"
        assert (done_dir / "TODO.md").is_file()
        notes = list(done_dir.glob("RETIRED-*.md"))
        assert len(notes) == 1
        assert "rejected at verify" in notes[0].read_text()
        # status no longer sees it
        rc2 = cli.main(["status", "--project-dir", str(repo)])
        out2 = capsys.readouterr().out
        assert rc2 == 0
        assert "TODO-0001" not in out2

    def test_retire_cli_without_reason_exits_2(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRetire2")
        self._ghost(repo)
        capsys.readouterr()

        with pytest.raises(SystemExit) as exc:
            cli.main(["todo-retire", "TODO-0001", "--project-dir", str(repo)])
        assert exc.value.code == 2
        assert (repo / ".agentic" / "inbox" / "TODO-0001.md").exists()

    def test_retire_cli_missing_todo_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliRetire3")
        capsys.readouterr()

        rc = cli.main(
            ["todo-retire", "TODO-0077", "--reason", "gone",
              "--project-dir", str(repo)]
        )

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "not found" in captured.out + captured.err


class TestTodoUpdateEntrypoint:
    """RUN6 #4: `awf todo-update` — real runs, no mocks."""

    def test_todo_update_cli_keeps_number_ready_baseline(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUpdate")
        capsys.readouterr()
        # dispatch-shaped TODO: .md + .ready, plus a baseline snapshot
        inbox = repo / ".agentic" / "inbox"
        (inbox / "TODO-0001.md").write_text("# Task\nold body\n")
        (inbox / "TODO-0001.ready").touch()
        (repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text("b" * 40)
        new_content = tmp_path / "new-content.md"
        new_content.write_text("# Task\nnew body\n", encoding="utf-8")

        rc = cli.main([
            "todo-update", "TODO-0001",
            "--content-file", str(new_content),
            "--reason", "reworded at plan",
            "--project-dir", str(repo),
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        # number + dispatch shape + baseline kept, content replaced
        assert (inbox / "TODO-0001.md").read_text() == "# Task\nnew body\n"
        assert (inbox / "TODO-0001.ready").is_file()
        assert (repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").read_text() == "b" * 40
        # backup of the old content in context/
        backups = list((repo / ".agentic" / "context").glob("TODO-0001.md.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "# Task\nold body\n"
        # log line with the reason
        log = (repo / ".agentic" / "logs" / "orchestrator.log").read_text()
        assert "todo-update: TODO-0001" in log
        assert "reworded at plan" in log

    def test_todo_update_cli_short_content_flag(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUpdate2")
        capsys.readouterr()
        (repo / ".agentic" / "inbox" / "TODO-0001.md").write_text("old")

        rc = cli.main([
            "todo-update", "TODO-0001", "--content", "short",
            "--project-dir", str(repo),
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        assert (repo / ".agentic" / "inbox" / "TODO-0001.md").read_text() == "short"

    def test_todo_update_cli_refuses_started(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUpdate3")
        capsys.readouterr()
        (repo / ".agentic" / "inbox" / "TODO-0001.md").write_text("in flight")
        (repo / ".agentic" / "inbox" / "TODO-0001.ready").touch()
        (repo / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("half")

        rc = cli.main([
            "todo-update", "TODO-0001", "--content", "v2",
            "--project-dir", str(repo),
        ])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "in flight" in captured.out + captured.err
        assert (repo / ".agentic" / "inbox" / "TODO-0001.md").read_text() == "in flight"

    def test_todo_update_cli_missing_todo_returns_1(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUpdate4")
        capsys.readouterr()

        rc = cli.main([
            "todo-update", "TODO-0077", "--content", "v2",
            "--project-dir", str(repo),
        ])

        captured = capsys.readouterr()
        assert rc == 1, captured.out
        assert "not found" in captured.out + captured.err

    def test_todo_update_cli_requires_content_source(self, tmp_path):
        """Neither --content nor --content-file: argparse exits 2 (owner
        mistake), and nothing is read or written."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliUpdate5")

        with pytest.raises(SystemExit) as exc:
            cli.main(["todo-update", "TODO-0001", "--project-dir", str(repo)])
        assert exc.value.code == 2
        assert not (repo / ".agentic" / "inbox" / "TODO-0001.md").exists()


class TestFeedbackEntrypoint:
    """RUN4 #2: `awf feedback` — real runs, no mocks (output redirected
    to tmp via config feedback.dir, never the real desktop)."""

    def _set_feedback_dir(self, repo: Path, dir_path: Path) -> None:
        cfg_file = repo / ".agentic" / "config.yaml"
        cfg = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        cfg["feedback"] = {"dir": str(dir_path)}
        cfg_file.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    def test_feedback_writes_report_to_configured_dir(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliFeedback")
        desk = tmp_path / "desk"
        self._set_feedback_dir(repo, desk)
        capsys.readouterr()

        rc = cli.main([
            "feedback", "--type", "bug", "--title", "Cli report",
            "--body", "что делал", "--project-dir", str(repo),
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        files = list(desk.glob("awf-bug-*.md"))
        assert len(files) == 1
        assert "Отчёт: " in out and files[0].name in out
        text = files[0].read_text(encoding="utf-8")
        assert "CliFeedback" in text
        assert "## Что пытался" in text
        assert "что делал" in text

    def test_feedback_stdout_prints_and_writes_nothing(self, tmp_path, capsys):
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliFeedbackStdout")
        desk = tmp_path / "desk"
        self._set_feedback_dir(repo, desk)
        capsys.readouterr()

        rc = cli.main([
            "feedback", "--type", "feature", "--title", "Only print",
            "--project-dir", str(repo), "--stdout",
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        # RUN10 #2: без текстов — ни одной пустой секции в отчёте
        for section in ("Что пытался", "Ожидал", "Что получил",
                        "Почему мешает", "Предложение"):
            assert f"## {section}" not in out
        assert "Only print" in out
        assert not desk.exists() or not list(desk.glob("*.md"))

    def test_feedback_section_flags_print_their_sections(self, tmp_path, capsys):
        """RUN10 #2: --expected/--got/--why/--proposal печатают секции."""
        repo = _git_repo(tmp_path)
        api.init_project(repo, project_name="CliFeedbackFlags")
        desk = tmp_path / "desk"
        self._set_feedback_dir(repo, desk)
        capsys.readouterr()

        rc = cli.main([
            "feedback", "--type", "bug", "--title", "All sections",
            "--body", "b", "--expected", "e", "--got", "g",
            "--why", "w", "--proposal", "p",
            "--project-dir", str(repo), "--stdout",
        ])

        out = capsys.readouterr().out
        assert rc == 0, out
        for heading in (
            "## Что пытался", "## Ожидал", "## Что получил",
            "## Почему мешает", "## Предложение",
        ):
            assert heading in out
        assert not desk.exists() or not list(desk.glob("*.md"))
