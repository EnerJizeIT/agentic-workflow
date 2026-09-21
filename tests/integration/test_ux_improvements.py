"""Tests for UX improvements П2/П3/П6/П7.

- П2: cmd_init writes plan.md stub pointing to vision/README
- П3: orchestrator auto-creates baseline SHA
- П6: supervisor.build_prompt injects vision excerpt
- П7: plan_checkpoint cleans up stale /tmp/ HTML
"""
from __future__ import annotations

from pathlib import Path

import pytest
from conftest import _git_init, _git_init_bare  # AUD12-08: shared git boilerplate

from awf import paths
from awf.pipeline_engine import _ensure_baseline_sha
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
# _git_init / _git_init_bare — из корневого conftest (AUD12-08).


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

    def test_handles_empty_git_repo(self, tmp_path):
        """T1 (QA gap): git init but no commits yet — current_sha fails.
        _ensure_baseline_sha must catch and log, not crash."""
        _git_init_bare(tmp_path)
        # NO commit — HEAD doesn't exist
        context = tmp_path / ".agentic" / "context"
        logs = tmp_path / ".agentic" / "logs"
        context.mkdir(parents=True)
        logs.mkdir(parents=True)

        # Must NOT raise — exception caught internally
        _ensure_baseline_sha(tmp_path, "TODO-0001", logs)

        # No SHA file created (git command failed)
        assert not (context / "BASELINE-TODO-0001.sha").exists()

        # Error logged
        log_content = (logs / "orchestrator.log").read_text(encoding="utf-8")
        assert "baseline creation failed" in log_content


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


class TestCheckpointHtmlStructure:
    """T5 (QA gap): structural integrity of dark-theme HTML form.

    The f-string template has many `{{` / `}}` for CSS braces — easy to
    break silently (e.g. unbalanced braces, missing closing tags, escaped
    variable by mistake). This test catches regressions before user sees
    a broken form.
    """

    def test_html_well_formed(self):
        """Render form, validate basic HTML structure."""
        from awf.plan_checkpoint import _render_html

        html = _render_html(
            todo_id="TODO-0042",
            todo_content="# Sample task\nDo work",
            plan_content="- [ ] Step 1",
            port=12345,
        )

        # Structural checks
        assert html.count("<html") == 1, "Exactly one <html>"
        assert html.count("</html>") == 1, "Exactly one </html>"
        assert html.count("<head>") == 1
        assert html.count("</head>") == 1
        assert html.count("<body>") == 1
        assert html.count("</body>") == 1
        assert html.count("<script>") == 1
        assert html.count("</script>") == 1

        # CSS braces balanced (every { has matching })
        # f-string doubles braces, so source has {{ }} — but after f-string
        # evaluation, output should have balanced single { }.
        open_braces = html.count("{")
        close_braces = html.count("}")
        assert open_braces == close_braces, (
            f"CSS/JS braces unbalanced: {open_braces} open vs {close_braces} close"
        )

    def test_dark_theme_variables_present(self):
        """T5: dark theme CSS variables must be defined."""
        from awf.plan_checkpoint import _render_html
        html = _render_html("T1", "x", "", 1)
        # Critical palette variables (must mirror project-setup.html.j2)
        for var in ("--bg", "--accent", "--accent-green", "--text", "--danger"):
            assert var in html, f"Missing CSS variable {var}"

    def test_all_three_decision_buttons_present(self):
        """T5: form must have approve/edit/reject buttons."""
        from awf.plan_checkpoint import _render_html
        html = _render_html("T1", "x", "", 1)
        assert "submitDecision('approve')" in html
        assert "submitDecision('edit')" in html
        assert "submitDecision('reject')" in html

    def test_no_double_escaped_braces(self):
        """T5: regression check — no leftover {{ or }} in output
        (would mean an f-string brace was meant as literal but escaped)."""
        from awf.plan_checkpoint import _render_html
        html = _render_html("T1", "x", "", 1)
        # In CSS/JS we expect single { and }. Doubled {{ would be a bug.
        # Allow them only inside <style> or <script> tag literals if needed,
        # but in our template all braces are CSS/JS syntax, not f-string literals.
        assert "{{" not in html, "Found {{ in output — f-string brace escaped by mistake"
        assert "}}" not in html, "Found }} in output — f-string brace escaped by mistake"


# ── _cleanup_stale_temp_html (П7) ────────────────────────────────────────────


class TestCleanupStaleTempHtml:
    @pytest.fixture(autouse=True)
    def _isolated_tempdir(self, tmp_path, monkeypatch):
        """U7a: point cleanup at a private dir. The real tempdir is shared
        across xdist workers — concurrent workers' awf-checkpoint-*.html
        pre-clean/unlink would race with this test's files (QA note from
        TODO-0013). Same pattern as test_plan_checkpoint.py::
        TestStaleCleanupProtectsLiveForm."""
        import tempfile

        isolated = tmp_path / "ckpt-tmp"
        isolated.mkdir()
        monkeypatch.setattr(tempfile, "gettempdir", lambda: str(isolated))
        yield

    def test_removes_existing_stale_files(self, tmp_path, monkeypatch):
        """П7: stale awf-checkpoint-*.html from crashed runs are removed
        (only if older than 10 min — protects concurrent awf instances)."""
        import os
        import tempfile
        import time

        tmp_dir = Path(tempfile.gettempdir())
        # Create fake stale files + age them past the 10-minute window
        stale1 = tmp_dir / "awf-checkpoint-TODO-0001-aaa.html"
        stale2 = tmp_dir / "awf-checkpoint-TODO-0002-bbb.html"
        stale1.write_text("old")
        stale2.write_text("older")
        # Set mtime to 1 hour ago (well past the 10-min safety window)
        old_time = time.time() - 3600
        os.utime(stale1, (old_time, old_time))
        os.utime(stale2, (old_time, old_time))

        try:
            removed = _cleanup_stale_temp_html(tmp_path)
            assert removed == 2
            assert not stale1.exists()
            assert not stale2.exists()
        finally:
            # Defensive cleanup in case test failed mid-way
            for f in (stale1, stale2):
                if f.exists():
                    f.unlink()

    def test_preserves_recent_files_from_concurrent_run(self, tmp_path, monkeypatch):
        """П7: files younger than 10 minutes are NOT removed — protects
        a concurrently-running awf instance that just opened its form."""
        import tempfile

        tmp_dir = Path(tempfile.gettempdir())
        recent = tmp_dir / "awf-checkpoint-TODO-0099-fresh.html"
        recent.write_text("just created by another awf instance")

        try:
            removed = _cleanup_stale_temp_html(tmp_path)
            # Recent file must survive
            assert recent.exists(), "Recent file (<10min) must NOT be removed"
            # removed count may be 0 or include other stale files from
            # earlier tests, but NOT our recent one.
        finally:
            if recent.exists():
                recent.unlink()

    def test_returns_zero_when_nothing_to_clean(self, tmp_path):
        """No stale files — returns 0, no error."""
        # Pre-clean to ensure clean state (isolated dir, see _isolated_tempdir)
        import glob
        import tempfile
        for f in glob.glob(f"{tempfile.gettempdir()}/awf-checkpoint-*.html"):
            Path(f).unlink(missing_ok=True)

        assert _cleanup_stale_temp_html(tmp_path) == 0

    def test_ignores_permission_errors(self, tmp_path, monkeypatch):
        """П7: permission errors are silently ignored (best-effort)."""
        import os
        import tempfile
        import time

        # Create one real STALE file in the ISOLATED tempdir (autouse
        # fixture) and age it past the 10-minute window (same pattern as
        # test_removes_existing_stale_files) so the cleanup actually
        # reaches the unlink call.
        stale = Path(tempfile.gettempdir()) / "awf-checkpoint-test-perm.html"
        stale.write_text("test")
        old_time = time.time() - 3600
        os.utime(stale, (old_time, old_time))

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
            removed = _cleanup_stale_temp_html(tmp_path)
            assert isinstance(removed, int)
            # The permission path was actually exercised (not skipped by
            # the age window) and the failed unlink was swallowed.
            assert call_count["n"] >= 1, "unlink was never attempted — test is a no-op"
            assert removed == 0, "failed unlink must not count as removed"
        finally:
            # Real cleanup (unpatched)
            monkeypatch.undo()
            if stale.exists():
                original_unlink(stale)
