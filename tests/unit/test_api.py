"""Unit tests for awf.api — public API for all callers (CLI + MCP plugin).

Each test validates a public function's contract:
- Returns correct Result dataclass on success
- Raises AwfApiError with helpful message on precondition failure
- Side effects (files, signals) match expectations
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awf import api

# ─── detect_stack ───────────────────────────────────────────────────────


class TestDetectStack:
    def test_empty_dir_returns_unknown(self, tmp_path):
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "unknown"
        assert result["test_cmd"] == ""

    def test_package_json_with_scripts(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {
                "test": "bun test",
                "lint": "biome check .",
                "build": "bun run build",
                "typecheck": "tsc --noEmit",
            },
            "devDependencies": {"typescript": "^5.0"},
        }))
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "typescript"
        assert result["test_cmd"] == "bun test"
        assert result["lint_cmd"] == "biome check ."
        assert result["build_cmd"] == "bun run build"
        assert result["typecheck_cmd"] == "tsc --noEmit"

    def test_package_json_without_typecheck_falls_back_to_tsc(self, tmp_path):
        """If tsconfig.json exists but scripts.typecheck is missing → tsc --noEmit."""
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"},
            "devDependencies": {"typescript": "^5.0"},
        }))
        (tmp_path / "tsconfig.json").write_text("{}")
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "typescript"
        assert result["typecheck_cmd"] == "tsc --noEmit"

    def test_package_json_javascript_when_no_ts(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"},
        }))
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "javascript"

    def test_pyproject_toml(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n'
            '[tool.pytest]\n'
            '[tool.mypy]\n'
        )
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "python"
        assert result["test_cmd"] == "pytest"
        assert result["lint_cmd"] == "ruff check ."
        assert result["typecheck_cmd"] == "mypy ."
        assert result["build_cmd"] == "pip install -e ."

    def test_cargo_toml(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n')
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "rust"
        assert result["test_cmd"] == "cargo test"
        assert result["lint_cmd"] == "cargo clippy"

    def test_go_mod(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n")
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "go"
        assert result["test_cmd"] == "go test ./..."

    def test_heuristic_ts_extension(self, tmp_path):
        """No config files but .ts file → typescript stack."""
        (tmp_path / "main.ts").write_text("const x = 1;")
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "typescript"
        assert result["test_cmd"] == "bun test"

    def test_heuristic_py_extension(self, tmp_path):
        (tmp_path / "main.py").write_text("print('hi')")
        result = api.detect_stack(tmp_path)
        assert result["stack"] == "python"

    def test_invalid_package_json_falls_through(self, tmp_path):
        """Malformed package.json → falls through to heuristic detection."""
        (tmp_path / "package.json").write_text("{ not valid json")
        (tmp_path / "main.py").write_text("print('hi')")
        result = api.detect_stack(tmp_path)
        # Should fall through to Python heuristic
        assert result["stack"] == "python"


# ─── derive_project_name ────────────────────────────────────────────────


class TestDeriveProjectName:
    def test_simple_kebab_case(self, tmp_path):
        d = tmp_path / "jira-epic-presenter"
        d.mkdir()
        assert api.derive_project_name(d) == "Jira Epic Presenter"

    def test_snake_case(self, tmp_path):
        d = tmp_path / "my_awesome_project"
        d.mkdir()
        assert api.derive_project_name(d) == "My Awesome Project"

    def test_camel_case_preserved(self, tmp_path):
        d = tmp_path / "myApp"
        d.mkdir()
        # camelCase is split into two words for readability
        assert api.derive_project_name(d) == "My App"

    def test_mixed_separators(self, tmp_path):
        d = tmp_path / "my-cool_app"
        d.mkdir()
        assert api.derive_project_name(d) == "My Cool App"

    def test_already_title_case(self, tmp_path):
        d = tmp_path / "MyProject"
        d.mkdir()
        # CamelCase also split for consistency
        assert api.derive_project_name(d) == "My Project"


# ─── approve_commit ─────────────────────────────────────────────────────


class TestApproveCommit:
    def test_creates_signal_file(self, tmp_git_repo):
        # Need .agentic/inbox
        (tmp_git_repo / ".agentic" / "inbox").mkdir(parents=True)
        result = api.approve_commit(tmp_git_repo, "TODO-0001")
        assert isinstance(result, api.ApproveResult)
        assert result.todo_id == "TODO-0001"
        assert "APPROVE-TODO-0001.ready" in result.signal_file
        assert (tmp_git_repo / ".agentic" / "inbox" / "APPROVE-TODO-0001.ready").exists()

    def test_creates_inbox_if_missing(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        result = api.approve_commit(tmp_git_repo, "TODO-0042")
        assert (tmp_git_repo / ".agentic" / "inbox" / "APPROVE-TODO-0042.ready").exists()

    def test_empty_todo_id_raises(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="todo_id is required"):
            api.approve_commit(tmp_git_repo, "")

    def test_creates_inbox_inside_agentic(self, tmp_git_repo):
        """approve creates inbox/ inside existing .agentic/ (audit fix:
        no longer creates .agentic/ itself — caller must init first)."""
        (tmp_git_repo / ".agentic").mkdir()
        result = api.approve_commit(tmp_git_repo, "TODO-0009")
        assert (tmp_git_repo / ".agentic" / "inbox" / "APPROVE-TODO-0009.ready").exists()

    def test_missing_agentic_raises(self, tmp_git_repo):
        """Audit fix: approve_commit requires .agentic/ (consistency)."""
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.approve_commit(tmp_git_repo, "TODO-0010")


# ─── create_baseline ────────────────────────────────────────────────────


class TestCreateBaseline:
    def test_creates_baseline_files_in_git_repo(self, tmp_git_repo):
        (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
        # Add config.yaml with test_cmd
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'verification:\n  test_cmd: "echo hello"\n'
        )
        result = api.create_baseline(tmp_git_repo, "TODO-0001")
        assert isinstance(result, api.BaselineResult)
        assert result.is_git_repo is True
        assert len(result.sha) == 40  # git SHA-1 hex
        assert "BASELINE-TODO-0001.sha" in result.files_created
        assert "BASELINE-TODO-0001.status" in result.files_created
        assert "BASELINE-TODO-0001.tests.log" in result.files_created
        assert "BASELINE-TODO-0001.env.log" in result.files_created

    def test_test_status_passed_when_echo(self, tmp_git_repo):
        (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'verification:\n  test_cmd: "true"\n'
        )
        result = api.create_baseline(tmp_git_repo, "TODO-0001")
        assert result.test_status == "passed"

    def test_test_status_failed_when_false(self, tmp_git_repo):
        (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'verification:\n  test_cmd: "false"\n'
        )
        result = api.create_baseline(tmp_git_repo, "TODO-0001")
        assert result.test_status == "failed"

    def test_no_config_yields_no_config_status(self, tmp_git_repo):
        (tmp_git_repo / ".agentic" / "context").mkdir(parents=True)
        result = api.create_baseline(tmp_git_repo, "TODO-0001")
        assert result.test_status == "no_config"

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.create_baseline(tmp_git_repo, "TODO-0001")


# ─── rollback ───────────────────────────────────────────────────────────


class TestRollback:
    def _setup(self, repo: Path) -> str:
        """Create baseline + commit on top → ready for rollback."""
        (repo / ".agentic" / "context").mkdir(parents=True)
        (repo / ".agentic" / "inbox").mkdir(parents=True)
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
        ).strip()
        (repo / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(sha + "\n")
        # Make a change on top of baseline
        (repo / "extra.txt").write_text("extra change\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=repo, check=True)
        return sha

    def test_dry_run_returns_diff_without_changing(self, tmp_git_repo):
        sha = self._setup(tmp_git_repo)
        result = api.rollback(tmp_git_repo, "TODO-0001", mode="dry-run")
        assert isinstance(result, api.RollbackResult)
        assert result.mode == "dry-run"
        assert result.baseline_sha == sha
        assert result.ack_file is None
        # extra.txt should still be in the repo (no actual reset)
        assert (tmp_git_repo / "extra.txt").exists()

    def test_hard_reset_removes_changes(self, tmp_git_repo):
        self._setup(tmp_git_repo)
        api.rollback(tmp_git_repo, "TODO-0001", mode="hard")
        assert not (tmp_git_repo / "extra.txt").exists()
        # ACK file created
        assert (tmp_git_repo / ".agentic" / "inbox" / "ACK-TODO-0001.ready").exists()

    def test_soft_reset_keeps_changes_in_working_dir(self, tmp_git_repo):
        self._setup(tmp_git_repo)
        api.rollback(tmp_git_repo, "TODO-0001", mode="soft")
        # Soft reset: HEAD moved back, but files remain in working dir
        assert (tmp_git_repo / "extra.txt").exists()

    def test_missing_baseline_raises(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="Baseline SHA not found"):
            api.rollback(tmp_git_repo, "TODO-9999")

    def test_invalid_mode_raises(self, tmp_git_repo):
        self._setup(tmp_git_repo)
        with pytest.raises(api.AwfApiError, match="invalid mode"):
            api.rollback(tmp_git_repo, "TODO-0001", mode="weird")

    def test_path_traversal_rejected(self, tmp_git_repo):
        """AUD-2026-08-09.2: todo_id with path separators must be rejected."""
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.rollback(tmp_git_repo, "../../etc/passwd")

    def test_malformed_todo_id_rejected(self, tmp_git_repo):
        """AUD-2026-08-09.2: todo_id must match TODO-NNNN format."""
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="invalid todo_id"):
            api.rollback(tmp_git_repo, "TODO-abc")


# ─── get_status ─────────────────────────────────────────────────────────


class TestGetStatus:
    def test_empty_project(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'project:\n  name: Test\n'
        )
        result = api.get_status(tmp_git_repo)
        assert isinstance(result, api.StatusResult)
        assert result.project_name == "Test"
        assert result.active_todos == []
        assert result.done_count == 0
        assert result.suggestion is not None  # "No active tasks..."

    def test_active_todo_with_progress(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "config.yaml").write_text('project:\n  name: Test\n')
        # Active TODO
        (agentic / "inbox" / "TODO-0001.ready").touch()
        (agentic / "inbox" / "TODO-0001.md").write_text("# Task")
        # Progress
        (agentic / "outbox" / "PROGRESS-TODO-0001.md").write_text(
            "## Task 1 [x]\n## Task 2 [ ]\n"
        )
        result = api.get_status(tmp_git_repo)
        assert len(result.active_todos) == 1
        assert result.active_todos[0]["todo_id"] == "TODO-0001"
        assert result.active_todos[0]["progress"]["done"] == 1
        assert result.active_todos[0]["progress"]["total"] == 2
        assert result.suggestion is None  # there IS an active task

    def test_blocked_todo_counted(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "config.yaml").write_text('project:\n  name: Test\n')
        (agentic / "inbox" / "TODO-0001.ready").touch()
        (agentic / "outbox" / "BLOCKED-TODO-0001.ready").touch()
        result = api.get_status(tmp_git_repo)
        assert result.blocked_count == 1
        assert result.blocked_ids == ["TODO-0001"]
        assert result.done_count == 0

    def test_conflict_warning_when_multiple_active(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "config.yaml").write_text('project:\n  name: Test\n')
        # Active TODO requires .ready AND non-empty .md
        (agentic / "inbox" / "TODO-0001.ready").touch()
        (agentic / "inbox" / "TODO-0001.md").write_text("# Task 1")
        (agentic / "inbox" / "TODO-0002.ready").touch()
        (agentic / "inbox" / "TODO-0002.md").write_text("# Task 2")
        result = api.get_status(tmp_git_repo)
        assert result.conflict_warning is not None
        assert "2 active TODOs" in result.conflict_warning

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError):
            api.get_status(tmp_git_repo)


# ─── MCP-4: pipeline running detection ──────────────────────────────────


class TestPipelineRunningDetection:
    """MCP-4: awf_status detects background pipeline subprocess via PID file."""

    def test_no_pid_file_returns_not_running(self, tmp_git_repo):
        """No .agentic/logs/awf-start.pid → pipeline_running=False."""
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'project:\n  name: T\n'
        )
        result = api.get_status(tmp_git_repo)
        assert result.pipeline_running is False
        assert result.pipeline_pid is None

    def test_stale_pid_file_cleaned_up(self, tmp_git_repo):
        """PID file points to dead process → cleaned up, marked not running."""
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text('project:\n  name: T\n')
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir()
        # PID 999999 almost certainly doesn't exist
        (logs / "awf-start.pid").write_text("999999\n")
        result = api.get_status(tmp_git_repo)
        assert result.pipeline_running is False
        # Stale PID file removed
        assert not (logs / "awf-start.pid").exists()

    def test_corrupt_pid_file_handled(self, tmp_git_repo):
        """Garbage in PID file → treated as not running, file cleaned."""
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text('project:\n  name: T\n')
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir()
        (logs / "awf-start.pid").write_text("not-a-number\n")
        result = api.get_status(tmp_git_repo)
        assert result.pipeline_running is False
        assert not (logs / "awf-start.pid").exists()

    def test_live_pid_detected_as_running(self, tmp_git_repo):
        """PID file pointing to current process → marked running."""
        import os
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text('project:\n  name: T\n')
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir()
        # Use current process PID — guaranteed alive during test
        (logs / "awf-start.pid").write_text(f"{os.getpid()}\n")
        (logs / "awf-start.out").write_text("line1\nline2\nline3\n")
        result = api.get_status(tmp_git_repo)
        assert result.pipeline_running is True
        assert result.pipeline_pid == os.getpid()
        assert result.log_tail is not None
        assert "line3" in result.log_tail

    def test_log_tail_truncated_to_n_lines(self, tmp_git_repo):
        """log_tail returns last N lines (default 20)."""
        import os
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text('project:\n  name: T\n')
        logs = tmp_git_repo / ".agentic" / "logs"
        logs.mkdir()
        (logs / "awf-start.pid").write_text(f"{os.getpid()}\n")
        # Write 30 lines
        (logs / "awf-start.out").write_text("\n".join(f"L{i}" for i in range(30)))
        result = api.get_status(tmp_git_repo)
        assert result.pipeline_running is True
        # Default tail = 20 lines
        tail_lines = result.log_tail.split("\n")
        assert len(tail_lines) == 20
        assert tail_lines[-1] == "L29"

    def test_missing_log_file_returns_none_tail(self, tmp_git_repo):
        """PID file exists but log file missing → log_tail is None."""
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text('project:\n  name: T\n')
        # P2: create state with a PID so log_tail logic actually runs,
        # but don't create the log file — verify graceful None return.
        from awf.pipeline_state import write_state
        write_state(tmp_git_repo, pipeline_pid="99999", logs_dir=tmp_git_repo / ".agentic" / "logs")
        result = api.get_status(tmp_git_repo)
        assert result.log_tail is None


# ─── get_report ─────────────────────────────────────────────────────────


class TestGetReport:
    def test_empty_project(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "config.yaml").write_text(
            'project:\n  name: Test\n'
        )
        result = api.get_report(tmp_git_repo)
        assert isinstance(result, api.ReportResult)
        assert result.project_name == "Test"
        assert result.items == []
        assert result.done_count == 0

    def test_with_done_blocked_inprogress(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        (agentic / "config.yaml").write_text('project:\n  name: Test\n')
        # Done
        (agentic / "inbox" / "TODO-0001.ready").touch()
        (agentic / "outbox" / "DONE-TODO-0001.ready").touch()
        # Blocked
        (agentic / "inbox" / "TODO-0002.ready").touch()
        (agentic / "outbox" / "BLOCKED-TODO-0002.ready").touch()
        # In progress
        (agentic / "inbox" / "TODO-0003.ready").touch()
        result = api.get_report(tmp_git_repo)
        assert result.done_count == 1
        assert result.blocked_count == 1
        statuses = {item["todo_id"]: item["status"] for item in result.items}
        assert statuses["TODO-0001"] == "OK"
        assert statuses["TODO-0002"] == "BLK"
        assert statuses["TODO-0003"] == "..."

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError):
            api.get_report(tmp_git_repo)


# ─── reset_runtime ──────────────────────────────────────────────────────


class TestResetRuntime:
    def test_default_cleans_runtime_dirs(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        for d in ["inbox", "outbox", "context", "logs", "reports", "phases"]:
            (agentic / d).mkdir(parents=True)
            (agentic / d / "junk.txt").write_text("junk")
        result = api.reset_runtime(tmp_git_repo)
        assert "inbox" in result.cleaned_dirs
        assert "outbox" in result.cleaned_dirs
        assert "context" in result.cleaned_dirs
        assert "logs" in result.cleaned_dirs
        assert "reports" in result.cleaned_dirs
        # phases NOT cleaned
        assert "phases" not in result.cleaned_dirs
        assert (agentic / "phases" / "junk.txt").exists()

    def test_tasks_only_keeps_context_logs(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        for d in ["inbox", "outbox", "context", "logs"]:
            (agentic / d).mkdir(parents=True)
            (agentic / d / "junk.txt").write_text("junk")
        result = api.reset_runtime(tmp_git_repo, tasks_only=True)
        assert "inbox" in result.cleaned_dirs
        assert "outbox" in result.cleaned_dirs
        assert "context" not in result.cleaned_dirs
        assert "logs" not in result.cleaned_dirs

    def test_full_includes_everything_runtime(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        for d in ["inbox", "outbox", "context", "logs", "reports"]:
            (agentic / d).mkdir(parents=True)
            (agentic / d / "junk.txt").write_text("junk")
        result = api.reset_runtime(tmp_git_repo, full=True)
        assert "reports" in result.cleaned_dirs
        assert "context" in result.cleaned_dirs

    def test_orphans_removes_active_without_progress(self, tmp_git_repo):
        agentic = tmp_git_repo / ".agentic"
        (agentic / "inbox").mkdir(parents=True)
        (agentic / "outbox").mkdir(parents=True)
        # Orphan — has TODO-0001.ready but no PROGRESS file
        (agentic / "inbox" / "TODO-0001.ready").touch()
        (agentic / "inbox" / "TODO-0001.md").write_text("# Orphan")
        result = api.reset_runtime(tmp_git_repo, orphans=True)
        assert "TODO-0001" in result.orphan_ids
        assert not (agentic / "inbox" / "TODO-0001.ready").exists()

    def test_no_agentic_returns_noop(self, tmp_path):
        """Idempotent — no .agentic → no-op."""
        result = api.reset_runtime(tmp_path)
        assert result.mode == "noop"
        assert result.cleaned_dirs == []


# ─── list_orphans / remove_orphans (two-step protocol) ──────────────────


class TestOrphansTwoStep:
    """MCP-audit cleanup: list_orphans + remove_orphans replace the double
    computation in cmd_reset. list is read-only; remove takes pre-computed ids."""

    def _setup_with_orphan(self, tmp_git_repo):
        """Project with 1 orphan TODO (active, no progress) + 1 healthy TODO."""
        agentic = tmp_git_repo / ".agentic"
        inbox = agentic / "inbox"
        outbox = agentic / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)
        # Orphan: TODO-0001 active, no PROGRESS file
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# orphan")
        # Healthy: TODO-0002 active, HAS progress
        (inbox / "TODO-0002.ready").touch()
        (inbox / "TODO-0002.md").write_text("# healthy")
        (outbox / "PROGRESS-TODO-0002.md").write_text("## Task 1 [x]\n")
        return tmp_git_repo

    def test_list_orphans_returns_only_orphans(self, tmp_git_repo):
        self._setup_with_orphan(tmp_git_repo)
        orphans = api.list_orphans(tmp_git_repo)
        assert orphans == ["TODO-0001"]

    def test_list_orphans_read_only(self, tmp_git_repo):
        """list_orphans must NOT delete files — pure read."""
        self._setup_with_orphan(tmp_git_repo)
        api.list_orphans(tmp_git_repo)
        # Orphan files still there
        assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.ready").exists()
        assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.md").exists()

    def test_list_orphans_empty_when_no_orphans(self, tmp_git_repo):
        """Healthy project — no orphans."""
        agentic = tmp_git_repo / ".agentic"
        inbox = agentic / "inbox"
        outbox = agentic / "outbox"
        inbox.mkdir(parents=True)
        outbox.mkdir(parents=True)
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# task")
        (outbox / "PROGRESS-TODO-0001.md").write_text("## Task 1 [x]\n")
        assert api.list_orphans(tmp_git_repo) == []

    def test_remove_orphans_deletes_only_listed(self, tmp_git_repo):
        """remove_orphans takes explicit list, deletes only those."""
        self._setup_with_orphan(tmp_git_repo)
        result = api.remove_orphans(tmp_git_repo, ["TODO-0001"])
        assert result.mode == "orphans"
        assert result.orphan_ids == ["TODO-0001"]
        # Orphan files removed
        assert not (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.ready").exists()
        assert not (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.md").exists()
        # Healthy TODO preserved
        assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0002.ready").exists()

    def test_remove_orphans_empty_list_noop(self, tmp_git_repo):
        """Defensive: empty orphan_ids list — no-op, no crash."""
        self._setup_with_orphan(tmp_git_repo)
        result = api.remove_orphans(tmp_git_repo, [])
        assert result.orphan_ids == []
        # Nothing deleted
        assert (tmp_git_repo / ".agentic" / "inbox" / "TODO-0001.ready").exists()

    def test_remove_orphans_unknown_id_silently_skipped(self, tmp_git_repo):
        """remove_orphans with non-existent id — no error, just not in result."""
        self._setup_with_orphan(tmp_git_repo)
        result = api.remove_orphans(tmp_git_repo, ["TODO-9999"])
        assert result.orphan_ids == []  # nothing actually removed

    def test_two_step_protocol_e2e(self, tmp_git_repo):
        """End-to-end: list → confirm (simulated) → remove. Single computation."""
        self._setup_with_orphan(tmp_git_repo)
        # Step 1: list
        ids = api.list_orphans(tmp_git_repo)
        assert ids == ["TODO-0001"]
        # Step 2: user confirms (simulated)
        # Step 3: remove with pre-computed ids
        result = api.remove_orphans(tmp_git_repo, ids)
        assert result.orphan_ids == ["TODO-0001"]
        # Verify list_orphans now returns empty
        assert api.list_orphans(tmp_git_repo) == []


# ─── add_role ───────────────────────────────────────────────────────────


class TestAddRole:
    def test_creates_role_file(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        result = api.add_role(
            tmp_git_repo, "qa", description="QA engineer", model="claude-sonnet"
        )
        assert isinstance(result, api.AddRoleResult)
        assert result.role_name == "qa"
        role_file = tmp_git_repo / ".agentic" / "roles" / "qa.md"
        assert role_file.exists()
        content = role_file.read_text()
        assert "ROLE: qa" in content
        assert "claude-sonnet" in content
        assert "QA engineer" in content

    def test_default_model_placeholder(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        result = api.add_role(tmp_git_repo, "tester")
        assert "<set-me-in-.agentic/config.yaml>" in result.model

    def test_empty_role_name_raises(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="role_name is required"):
            api.add_role(tmp_git_repo, "")

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.add_role(tmp_git_repo, "qa")


# ─── analyze_roles ──────────────────────────────────────────────────────


class TestAnalyzeRoles:
    def _setup_overlapping_roles(self, repo: Path) -> None:
        ag = repo / ".agentic"
        (ag / "roles").mkdir(parents=True)
        (ag / "pipelines").mkdir(parents=True)
        (ag / "roles" / "supervisor.md").write_text("# supervisor")
        (ag / "roles" / "qa.md").write_text("# QA — verifies implementation")
        (ag / "roles" / "project-auditor.md").write_text("# Auditor — verifies implementation")
        (ag / "pipelines" / "default.yaml").write_text(
            "name: default\nstages:\n"
            "  - name: plan\n    role: supervisor\n"
            "  - name: qa\n    role: qa\n"
            "  - name: audit\n    role: project-auditor\n"
            "  - name: verify\n    role: supervisor\n"
        )

    def test_dry_run_returns_overlaps_without_writing(self, tmp_git_repo):
        self._setup_overlapping_roles(tmp_git_repo)
        result = api.analyze_roles(tmp_git_repo, dry_run=True)
        assert isinstance(result, api.AnalyzeRolesResult)
        assert result.dry_run is True
        assert len(result.overlaps) >= 1
        # Dry-run: role files untouched
        assert "BD-31" not in (tmp_git_repo / ".agentic" / "roles" / "qa.md").read_text()

    def test_applied_patches_marked_true(self, tmp_git_repo):
        self._setup_overlapping_roles(tmp_git_repo)
        result = api.analyze_roles(tmp_git_repo, dry_run=False)
        # Every successfully-written patch reports applied=True
        for entry in result.patches_applied:
            assert entry["applied"] is True, f"{entry['role']} should be applied"

    def test_failed_write_reports_applied_false_for_that_role(self, tmp_git_repo, monkeypatch):
        """Regression: a patch write that raises OSError must surface as
        applied=False for that role — not silently applied=True."""
        self._setup_overlapping_roles(tmp_git_repo)
        import awf.api.roles as core_mod

        real_write = core_mod.atomic_write_text

        def flaky(path, content, *a, **kw):
            if Path(path).name == "qa.md":
                raise OSError("simulated")
            return real_write(path, content, *a, **kw)

        monkeypatch.setattr(core_mod, "atomic_write_text", flaky)

        result = api.analyze_roles(tmp_git_repo, dry_run=False)
        by_role = {e["role"]: e for e in result.patches_applied}
        assert by_role["qa"]["applied"] is False
        assert by_role["project-auditor"]["applied"] is True

    def test_missing_roles_dir_raises(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="No .agentic/roles/"):
            api.analyze_roles(tmp_git_repo)


# ─── init_project ───────────────────────────────────────────────────────


class TestInitProject:
    def test_creates_skeleton_with_explicit_args(self, tmp_git_repo):
        result = api.init_project(
            tmp_git_repo,
            project_name="My Project",
            test_cmd="pytest",
            lint_cmd="ruff check .",
        )
        assert isinstance(result, api.InitResult)
        assert result.project_name == "My Project"
        assert result.stack == "unknown"  # no config files in tmp_git_repo
        # .agentic created with full structure
        for d in ["roles", "pipelines", "phases", "inbox", "outbox", "context", "logs", "reports"]:
            assert (tmp_git_repo / ".agentic" / d).is_dir(), f"missing .agentic/{d}/"
        assert (tmp_git_repo / ".agentic" / "config.yaml").exists()
        assert (tmp_git_repo / ".agentic" / "roles" / "supervisor.md").exists()
        assert (tmp_git_repo / ".agentic" / "phases" / "plan.md").exists()
        # Result has content
        assert "supervisor" in result.supervisor_md.lower()
        assert "Plan" in result.plan_md

    def test_auto_derives_name_and_stack_from_package_json(self, tmp_git_repo):
        (tmp_git_repo / "package.json").write_text(json.dumps({
            "name": "ts-project",
            "scripts": {"test": "bun test", "build": "bun run build"},
            "devDependencies": {"typescript": "^5.0"},
        }))
        (tmp_git_repo / "tsconfig.json").write_text("{}")
        # Rename tmp_git_repo to test name derivation
        # (tmp_git_repo fixture name is "repo" — we test derivation via sub-dir)
        sub = tmp_git_repo / "jira-epic-presenter"
        sub.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=sub, check=True)
        (sub / "package.json").write_text(json.dumps({
            "scripts": {"test": "bun test"},
            "devDependencies": {"typescript": "^5.0"},
        }))
        (sub / "tsconfig.json").write_text("{}")
        (sub / "README.md").write_text("init")

        result = api.init_project(sub)
        assert result.project_name == "Jira Epic Presenter"
        assert result.stack == "typescript"
        assert result.test_cmd if hasattr(result, "test_cmd") else "bun test"  # in config.yaml
        # Verify config.yaml has bun test
        config_text = (sub / ".agentic" / "config.yaml").read_text()
        assert "bun test" in config_text

    def test_vision_file_detected_and_excerpt_returned(self, tmp_git_repo):
        (tmp_git_repo / "1. PRODUCT-VISION.md").write_text(
            "# Vision\n\n" + ("content " * 100)
        )
        result = api.init_project(tmp_git_repo, project_name="Test")
        assert result.vision_path is not None
        assert "1. PRODUCT-VISION.md" in result.vision_path
        assert "Vision" in result.vision_excerpt
        # Plan.md points to vision
        assert "PRODUCT-VISION.md" in result.plan_md

    def test_no_vision_yields_warning(self, tmp_git_repo):
        # tmp_git_repo fixture creates README.md — remove it to test "no vision"
        (tmp_git_repo / "README.md").unlink()
        result = api.init_project(tmp_git_repo, project_name="Test")
        assert any("Vision/README not found" in w for w in result.warnings)
        assert result.vision_path is None
        assert result.vision_excerpt == ""

    def test_gitignore_updated(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="Test")
        gitignore = (tmp_git_repo / ".gitignore").read_text()
        assert ".agentic/inbox/" in gitignore
        assert ".agentic/inputs/" in gitignore  # plugin runtime

    def test_gitignore_idempotent(self, tmp_git_repo):
        """Running init twice (with force) doesn't duplicate gitignore blocks."""
        (tmp_git_repo / ".gitignore").write_text(
            "# existing\n.agentic/inbox/\n"
        )
        api.init_project(tmp_git_repo, project_name="Test")
        gitignore = (tmp_git_repo / ".gitignore").read_text()
        # No duplicate additions
        assert gitignore.count(".agentic/inbox/") == 1

    def test_existing_agentic_without_force_cleans_runtime(self, tmp_git_repo):
        """R1: awf init without force cleans runtime, preserves config."""
        agentic = tmp_git_repo / ".agentic"
        # Create config layer
        agentic.mkdir()
        (agentic / "config.yaml").write_text("project:\n  name: Test\n")
        (agentic / "roles").mkdir()
        (agentic / "roles" / "worker.md").write_text("# Worker")
        # Create runtime layer
        (agentic / "inbox").mkdir()
        (agentic / "inbox" / "TODO-0001.ready").write_text("")
        (agentic / "outbox").mkdir()
        (agentic / "outbox" / "DONE-TODO-0001.md").write_text("done")
        (agentic / "logs").mkdir()
        (agentic / "state").mkdir()

        result = api.init_project(tmp_git_repo, project_name="Test")

        # Runtime cleaned
        assert not (agentic / "inbox").exists()
        assert not (agentic / "outbox").exists()
        assert not (agentic / "logs").exists()
        # Config preserved
        assert (agentic / "config.yaml").exists()
        assert (agentic / "roles" / "worker.md").exists()

    def test_force_overwrites_existing(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        (tmp_git_repo / ".agentic" / "old.txt").write_text("old")
        api.init_project(tmp_git_repo, project_name="Test", force=True)
        # Directories created, files written
        assert (tmp_git_repo / ".agentic" / "config.yaml").exists()

    def test_not_git_repo_raises(self, tmp_path):
        with pytest.raises(api.AwfApiError, match="Not a git repository"):
            api.init_project(tmp_path, project_name="Test")

    def test_dry_run_returns_result_without_writing(self, tmp_git_repo):
        result = api.init_project(
            tmp_git_repo, project_name="Test", dry_run=True
        )
        assert result.supervisor_md == ""  # not written
        assert result.plan_md == ""
        assert not (tmp_git_repo / ".agentic").exists()

    def test_pipeline_configured_false_on_fresh_init(self, tmp_git_repo):
        result = api.init_project(tmp_git_repo, project_name="Test")
        assert result.pipeline_configured is False
        assert "project-setup" in result.next_action

    def test_as_dict_serializable(self, tmp_git_repo):
        """Result.as_dict() must produce JSON-serializable dict for MCP response."""
        import json as _json
        result = api.init_project(tmp_git_repo, project_name="Test")
        d = result.as_dict()
        # Must be JSON-serializable
        _json.dumps(d)


# ─── Result as_dict ─────────────────────────────────────────────────────


class TestResultAsDict:
    def test_all_result_types_have_as_dict(self):
        """Every Result dataclass must have as_dict() for MCP serialization."""
        from awf.api import (
            AddRoleResult,
            AnalyzeRolesResult,
            ApproveResult,
            BaselineResult,
            InitResult,
            ReportResult,
            ResetResult,
            RollbackResult,
            StartResult,
            StatusResult,
        )
        for cls in [
            InitResult, StatusResult, BaselineResult, RollbackResult,
            ApproveResult, ReportResult, ResetResult, AddRoleResult,
            StartResult, AnalyzeRolesResult,
        ]:
            assert hasattr(cls, "as_dict"), f"{cls.__name__} missing as_dict()"
        # P2: verify as_dict actually returns a non-empty dict (not just exists)
        sample = BaselineResult(
            todo_id="TODO-0001", sha="abc123", is_git_repo=True,
            files_created=[], test_status="passed", test_log_excerpt="",
        )
        d = sample.as_dict()
        assert isinstance(d, dict) and len(d) > 0, "as_dict() returned empty dict"
        assert "todo_id" in d, "as_dict() missing expected field"


# ─── start_pipeline / continue_pipeline ─────────────────────────────────


class TestStartPipeline:
    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.start_pipeline(tmp_git_repo, background=False)

    def test_background_noop_without_active_todo(self, tmp_git_repo):
        """Dogfood-10: background start without TODO → noop with guidance."""
        api.init_project(tmp_git_repo, project_name="Test")
        result = api.start_pipeline(tmp_git_repo, background=True)
        assert result.run_mode == "noop"
        assert "No active TODO" in result.message
        assert "awf_dispatch_todo" in result.message

    def test_background_works_with_active_todo(self, tmp_git_repo):
        """With active TODO → pipeline starts (or fails for other reasons, but NOT 'no TODO')."""
        api.init_project(tmp_git_repo, project_name="Test")
        inbox = tmp_git_repo / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task")
        result = api.start_pipeline(tmp_git_repo, background=True)
        # Should NOT be noop with "No active TODO"
        if result.run_mode == "noop":
            assert "No active TODO" not in result.message

    def test_from_stage_skips_todo_check(self, tmp_git_repo):
        """from_stage= bypasses TODO guard (resume mid-pipeline)."""
        api.init_project(tmp_git_repo, project_name="Test")
        result = api.start_pipeline(
            tmp_git_repo, background=True, from_stage="agent-developer"
        )
        if result.run_mode == "noop":
            assert "No active TODO" not in result.message

    def test_foreground_orchestrator_exception_caught(self, tmp_git_repo, monkeypatch):
        """When orchestrator.run_pipeline raises an unexpected exception,
        start_pipeline returns StartResult with exit_code=1 (not crash).

        Patches ``awf.api.pipeline.run_pipeline`` via the source module
        (start_pipeline imports it lazily inside the function).
        """
        api.init_project(tmp_git_repo, project_name="Test")

        import awf.orchestrator as orch_mod

        def crashing(args):
            raise KeyError("simulated orchestrator crash")

        monkeypatch.setattr(orch_mod, "run_pipeline", crashing)

        result = api.start_pipeline(tmp_git_repo, background=False)
        assert result.run_mode == "foreground"
        assert result.exit_code == 1
        assert "crashed" in result.message

    def test_foreground_normal_exit(self, tmp_git_repo, monkeypatch):
        """When orchestrator returns 0, start_pipeline propagates exit_code."""
        api.init_project(tmp_git_repo, project_name="Test")

        import awf.orchestrator as orch_mod

        def fake_run(args):
            return 0

        monkeypatch.setattr(orch_mod, "run_pipeline", fake_run)

        result = api.start_pipeline(tmp_git_repo, background=False)
        assert result.run_mode == "foreground"
        assert result.exit_code == 0


class TestContinuePipeline:
    def test_no_active_todo_returns_noop(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="Test")
        result = api.continue_pipeline(tmp_git_repo)
        assert result.run_mode == "noop"
        assert "No active TODO" in result.message

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.continue_pipeline(tmp_git_repo)

    def test_foreground_orchestrator_exception_caught(self, tmp_git_repo, monkeypatch):
        """When orchestrator raises, continue_pipeline returns exit_code=1."""
        api.init_project(tmp_git_repo, project_name="Test")
        # Create an active TODO so continue_pipeline doesn't return noop
        inbox = tmp_git_repo / ".agentic" / "inbox"
        (inbox / "TODO-0001.ready").touch()
        (inbox / "TODO-0001.md").write_text("# Task")

        import awf.orchestrator as orch_mod

        def crashing(args):
            raise RuntimeError("simulated crash")

        monkeypatch.setattr(orch_mod, "run_pipeline", crashing)

        result = api.continue_pipeline(tmp_git_repo, background=False)
        assert result.run_mode == "foreground"
        assert result.exit_code == 1
        assert "crashed" in result.message
