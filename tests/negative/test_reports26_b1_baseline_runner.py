"""REPORTS26 B1: baseline runner — package-manager routing, graceful
FileNotFoundError, zero residue on crash.

Source: awf-bug-20261007-baseline-auto-detected-test-cmd-vitest-run-crashes-with
(project jql-status-time-counter). `awf_init` stored the raw script body
(`vitest run`) as test_cmd; dispatch died with a raw FileNotFoundError
(the bare binary is not in PATH) and left BASELINE-TODO-9000.* residue
in .agentic/context/.

Three invariants:
(a) package.json scripts are routed through the package manager — picked
    by the lockfile in project_dir — instead of the bare binary;
(b) a missing command at baseline time is a recorded failure with a PATH
    hint in tests.log, not an exception — dispatch completes;
(c) an unexpected exception inside create_baseline after the first write
    leaves no BASELINE-<id>.* file written by that call behind.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import api
from awf.api._stack import detect_stack
from awf.api.dispatch import dispatch_todo
from awf.api.pipeline import create_baseline

MISSING_CMD = "definitely-not-a-real-cmd-xyz"


class TestJsCommandsRouteThroughPackageManager:
    """(a) scripts.* → `<pm> test` / `<pm> run <script>`; pm from lockfile."""

    @pytest.mark.parametrize(
        "lockfiles,pm",
        [
            ([], "npm"),  # no lockfile at all → npm
            (["package-lock.json"], "npm"),
            (["yarn.lock"], "yarn"),
            (["pnpm-lock.yaml"], "pnpm"),
            (["bun.lockb"], "bun"),
            (["bun.lock"], "bun"),
            (["bun.lock", "yarn.lock"], "bun"),  # bun wins the priority
        ],
    )
    def test_lockfile_selects_package_manager(self, tmp_path, lockfiles, pm):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run", "lint": "eslint ."},
        }), encoding="utf-8")
        for name in lockfiles:
            (tmp_path / name).write_text("", encoding="utf-8")
        result = detect_stack(tmp_path)
        assert result["test_cmd"] == f"{pm} test"
        assert result["lint_cmd"] == f"{pm} run lint"

    def test_absent_scripts_stay_empty(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "vitest run"},
        }), encoding="utf-8")
        result = detect_stack(tmp_path)
        assert result["test_cmd"] == "npm test"
        assert result["lint_cmd"] == ""
        assert result["build_cmd"] == ""
        assert result["typecheck_cmd"] == ""

    def test_tsconfig_fallback_unchanged(self, tmp_path):
        """No scripts.typecheck + tsconfig.json → tsc --noEmit, as before."""
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"},
        }), encoding="utf-8")
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        result = detect_stack(tmp_path)
        assert result["typecheck_cmd"] == "tsc --noEmit"


class TestMissingCommandIsFailureNotException:
    """(b) FileNotFoundError from run_tree → failed status + PATH hint."""

    @pytest.fixture
    def js_project(self, tmp_git_repo: Path) -> Path:
        api.init_project(tmp_git_repo, project_name="Reports26", test_cmd=MISSING_CMD)
        return tmp_git_repo

    def test_create_baseline_records_failure(self, js_project: Path):
        result = create_baseline(js_project, "TODO-9000")
        assert result.test_status == "failed"
        assert f"test_cmd не найдена в PATH: {MISSING_CMD}" in result.test_log_excerpt
        log = js_project / ".agentic" / "context" / "BASELINE-TODO-9000.tests.log"
        assert log.is_file()
        text = log.read_text(encoding="utf-8")
        assert f"test_cmd не найдена в PATH: {MISSING_CMD}" in text
        assert "Поправьте verification.baseline_cmd/test_cmd" in text

    def test_dispatch_todo_completes(self, js_project: Path):
        result = dispatch_todo(js_project, "# b1 missing cmd\n\nbody.\n")
        assert result.todo_id == "TODO-0001"
        context = js_project / ".agentic" / "context"
        log = context / "BASELINE-TODO-0001.tests.log"
        assert "не найдена в PATH" in log.read_text(encoding="utf-8")
        # A failed test is still a successful baseline — files stay.
        assert (context / "BASELINE-TODO-0001.sha").is_file()
        assert (js_project / ".agentic" / "inbox" / "TODO-0001.md").is_file()
        assert (js_project / ".agentic" / "inbox" / "TODO-0001.ready").is_file()


class TestBaselineCrashLeavesNoResidue:
    """(c) unexpected exception after the first write → zero residue."""

    def test_unexpected_exception_cleans_up_own_files(
        self, tmp_git_repo: Path, monkeypatch
    ):
        api.init_project(tmp_git_repo, project_name="Reports26R", test_cmd="echo ok")
        import awf.api.pipeline as pipeline_mod

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom-residue-test")

        # Env stage (after .sha/.status/.untracked/.tests.log are written).
        monkeypatch.setattr(pipeline_mod.shutil, "which", _boom)
        with pytest.raises(RuntimeError, match="boom-residue-test"):
            create_baseline(tmp_git_repo, "TODO-9003")
        residue = list(
            (tmp_git_repo / ".agentic" / "context").glob("BASELINE-TODO-9003.*")
        )
        assert residue == []

    def test_successful_baseline_keeps_files(self, tmp_git_repo: Path):
        api.init_project(tmp_git_repo, project_name="Reports26K", test_cmd="echo ok")
        create_baseline(tmp_git_repo, "TODO-9004")
        context = tmp_git_repo / ".agentic" / "context"
        assert sorted(p.name for p in context.glob("BASELINE-TODO-9004.*")) == [
            "BASELINE-TODO-9004.env.log",
            "BASELINE-TODO-9004.sha",
            "BASELINE-TODO-9004.status",
            "BASELINE-TODO-9004.tests.log",
            "BASELINE-TODO-9004.untracked",
        ]
