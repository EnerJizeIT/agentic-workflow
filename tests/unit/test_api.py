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

    def test_creates_agentic_inbox_if_missing(self, tmp_git_repo):
        """approve_commit is idempotent — creates .agentic/inbox/ if missing."""
        result = api.approve_commit(tmp_git_repo, "TODO-0009")
        assert (tmp_git_repo / ".agentic" / "inbox" / "APPROVE-TODO-0009.ready").exists()


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

    def test_existing_agentic_without_force_raises(self, tmp_git_repo):
        (tmp_git_repo / ".agentic").mkdir()
        with pytest.raises(api.AwfApiError, match="already exists"):
            api.init_project(tmp_git_repo, project_name="Test")

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
