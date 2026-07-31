"""Tests for UX improvements П2/П3/П6/П7.

- П2: cmd_init writes plan.md stub pointing to vision/README
- П3: orchestrator auto-creates baseline SHA
- П6: supervisor.build_prompt injects vision excerpt
- П7: plan_checkpoint cleans up stale /tmp/ HTML
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import paths
from awf.orchestrator import _ensure_baseline_sha
from awf.plan_checkpoint import _cleanup_stale_temp_html
from awf.supervisor import build_prompt

# ── find_vision_file (П2 helper, also used by П6) ────────────────────────────


class TestFindVisionFile:
    def test_finds_numbered_product_vision(self, tmp_path):
        """'1. PRODUCT-VISION.md' wins over plain 'PRODUCT-VISION.md'."""
        (tmp_path / "README.md").write_text("readme")
        (tmp_path / "1. PRODUCT-VISION.md").write_text("vision")
        result = paths.find_vision_file(tmp_path)
        assert result is not None
        assert result.name == "1. PRODUCT-VISION.md"

    def test_falls_back_to_readme(self, tmp_path):
        """If no vision file, README.md is used."""
        (tmp_path / "README.md").write_text("readme")
        result = paths.find_vision_file(tmp_path)
        assert result is not None
        assert result.name == "README.md"

    def test_returns_none_when_nothing_found(self, tmp_path):
        """Empty project — no vision candidates."""
        assert paths.find_vision_file(tmp_path) is None

    def test_case_insensitive_readme(self, tmp_path):
        """Lowercase readme.md is also accepted."""
        (tmp_path / "readme.md").write_text("readme")
        result = paths.find_vision_file(tmp_path)
        assert result is not None
        assert result.name == "readme.md"


# ── _ensure_baseline_sha (П3) ────────────────────────────────────────────────


def _git_init(proj: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=proj, check=True)
    (proj / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)


class TestEnsureBaselineSha:
    def test_creates_baseline_when_missing(self, tmp_path):
        """П3: when BASELINE-TODO.sha is missing, awf creates it from HEAD."""
        _git_init(tmp_path)
        context = tmp_path / ".agentic" / "context"
        logs = tmp_path / ".agentic" / "logs"
        context.mkdir(parents=True)
        logs.mkdir(parents=True)

        _ensure_baseline_sha(tmp_path, "TODO-0001", logs)

        sha_file = context / "BASELINE-TODO-0001.sha"
        assert sha_file.is_file()
        sha = sha_file.read_text(encoding="utf-8").strip()
        assert len(sha) == 40, "Must be a full git SHA"

    def test_skips_when_baseline_already_exists(self, tmp_path):
        """П3: if user/supervisor created baseline via `awf baseline`,
        orchestrator leaves it alone (idempotent)."""
        _git_init(tmp_path)
        context = tmp_path / ".agentic" / "context"
        logs = tmp_path / ".agentic" / "logs"
        context.mkdir(parents=True)
        logs.mkdir(parents=True)

        # Pre-existing baseline (e.g. richer version from `awf baseline`
        # with tests.log, env.log, etc.)
        existing_sha = "existing_sha_content_12345"
        (context / "BASELINE-TODO-0001.sha").write_text(existing_sha + "\n")

        _ensure_baseline_sha(tmp_path, "TODO-0001", logs)

        # Content unchanged
        assert (
            (context / "BASELINE-TODO-0001.sha").read_text(encoding="utf-8")
            == existing_sha + "\n"
        )

    def test_skips_when_not_git_repo(self, tmp_path):
        """П3: non-git project — no baseline, no error."""
        context = tmp_path / ".agentic" / "context"
        logs = tmp_path / ".agentic" / "logs"
        context.mkdir(parents=True)
        logs.mkdir(parents=True)

        _ensure_baseline_sha(tmp_path, "TODO-0001", logs)
        assert not (context / "BASELINE-TODO-0001.sha").exists()

    def test_skips_when_no_todo_id(self, tmp_path):
        """Defensive: empty todo_id — no-op."""
        _git_init(tmp_path)
        context = tmp_path / ".agentic" / "context"
        logs = tmp_path / ".agentic" / "logs"
        context.mkdir(parents=True)
        logs.mkdir(parents=True)

        _ensure_baseline_sha(tmp_path, "", logs)
        assert not list(context.glob("BASELINE-*.sha"))


# ── build_prompt vision injection (П6) ───────────────────────────────────────


class TestBuildPromptVisionInjection:
    def test_plan_prompt_includes_vision_excerpt(self, tmp_path):
        """П6: plan prompt auto-injects vision content."""
        vision = tmp_path / "1. PRODUCT-VISION.md"
        vision.write_text(
            "# Vision\nBuild a Chrome extension for Jira.\n",
            encoding="utf-8",
        )
        prompt = build_prompt("plan", "TODO-0001", project_dir=tmp_path)
        assert "Chrome extension for Jira" in prompt
        assert "1. PRODUCT-VISION.md" in prompt

    def test_plan_prompt_truncates_long_vision(self, tmp_path):
        """Long vision is truncated to keep prompt budget manageable."""
        vision = tmp_path / "VISION.md"
        # 10000 chars — well over the 4000 limit
        vision.write_text("X" * 10000, encoding="utf-8")
        prompt = build_prompt("plan", "TODO-0001", project_dir=tmp_path)
        assert "[... truncated ...]" in prompt
        # Truncated excerpt + boilerplate < full vision
        assert len(prompt) < 10000

    def test_plan_prompt_without_project_dir_backward_compat(self):
        """П6: project_dir is optional. Without it, no vision injection."""
        prompt = build_prompt("plan", "TODO-0001", project_dir=None)
        # Generic supervisor prompt still present
        assert "You are the supervisor" in prompt
        # No vision section
        assert "Project vision (auto-injected" not in prompt

    def test_plan_prompt_no_vision_file_in_project(self, tmp_path):
        """Empty project — no vision. Prompt still works (no excerpt)."""
        prompt = build_prompt("plan", "TODO-0001", project_dir=tmp_path)
        assert "You are the supervisor" in prompt
        assert "Project vision (auto-injected" not in prompt

    def test_verify_prompt_does_not_get_vision(self, tmp_path):
        """П6: vision only injected for plan stage. Verify stays focused."""
        (tmp_path / "VISION.md").write_text("project vision", encoding="utf-8")
        prompt = build_prompt("verify", "TODO-0001", project_dir=tmp_path)
        assert "project vision" not in prompt


# ── _cleanup_stale_temp_html (П7) ────────────────────────────────────────────


class TestCleanupStaleTempHtml:
    def test_removes_existing_stale_files(self, tmp_path, monkeypatch):
        """П7: stale /tmp/awf-checkpoint-*.html from crashed runs are removed."""
        import tempfile

        tmp_dir = Path(tempfile.gettempdir())
        # Create fake stale files
        stale1 = tmp_dir / "awf-checkpoint-TODO-0001-aaa.html"
        stale2 = tmp_dir / "awf-checkpoint-TODO-0002-bbb.html"
        stale1.write_text("old")
        stale2.write_text("older")

        try:
            removed = _cleanup_stale_temp_html()
            assert removed == 2
            assert not stale1.exists()
            assert not stale2.exists()
        finally:
            # Defensive cleanup in case test failed mid-way
            for f in (stale1, stale2):
                if f.exists():
                    f.unlink()

    def test_returns_zero_when_nothing_to_clean(self):
        """No stale files — returns 0, no error."""
        # Pre-clean to ensure clean state
        import glob
        for f in glob.glob("/tmp/awf-checkpoint-*.html"):
            Path(f).unlink(missing_ok=True)

        assert _cleanup_stale_temp_html() == 0

    def test_ignores_permission_errors(self, tmp_path, monkeypatch):
        """П7: permission errors are silently ignored (best-effort)."""
        # Patch Path.unlink to raise
        class _UnlinkablePath(type(Path())):
            def unlink(self, *args, **kwargs):
                raise PermissionError("denied")

        # Simpler: just verify the function doesn't propagate exceptions
        # when a permission error occurs. We create a real stale file
        # then monkeypatch glob.glob to return a fake path that raises.

        # Create one real stale file
        stale = Path("/tmp/awf-checkpoint-test-perm.html")
        stale.write_text("test")

        original_unlink = Path.unlink
        call_count = {"n": 0}

        def failing_unlink(self, *args, **kwargs):
            call_count["n"] += 1
            if "awf-checkpoint-test-perm" in str(self):
                raise PermissionError("simulated")
            return original_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", failing_unlink)

        try:
            # Must NOT raise — permission errors are caught internally
            removed = _cleanup_stale_temp_html()
            # The file we created counts as removed from glob's perspective,
            # even though unlink failed — that's fine, it's best-effort.
            assert isinstance(removed, int)
        finally:
            # Real cleanup (unpatched)
            monkeypatch.undo()
            if stale.exists():
                original_unlink(stale)
