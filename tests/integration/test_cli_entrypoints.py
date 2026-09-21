"""NEG-2026-09-19 · CLI entry points: real runs, no mocks.

The worst coverage holes were exactly the entry points an owner touches
first (cmd_init 0%, cmd_status 10%, dashboard_server 36%). These tests run
the real code paths against real temp projects.
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest
from conftest import _git_init  # AUD12-08: shared git boilerplate

from awf import api, cli


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
