"""Tests for dashboard generation — template rendering + error visibility (DF5-7/DF5-8).

Covers:
- generate_dashboard renders without exception (DF5-7 regression)
- Dashboard writes HTML file to correct path
- Template syntax is valid (no missing braces)
- Error logging on failure (DF5-8 — errors must not be silent)
"""
from __future__ import annotations

import pytest

from awf import api
from awf.api.dashboard import generate_dashboard, generate_state_dict
from awf.pipeline_state import read_state, write_state


@pytest.fixture
def dash_project(tmp_git_repo):
    """Project with .agentic/ + state file + pipeline.yaml."""
    api.init_project(tmp_git_repo, project_name="DashTest")

    # Minimal pipeline.yaml
    pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
    pipes_dir.mkdir(parents=True, exist_ok=True)
    (pipes_dir / "default.yaml").write_text(
        "stages:\n  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: agent-dev\n    role: developer\n    kind: execute\n"
        "  - name: verify\n    role: supervisor\n    kind: verify\n",
        encoding="utf-8",
    )

    # State file
    write_state(
        tmp_git_repo,
        stage_idx=1,
        stage_name="agent-dev",
        stage_kind="execute",
        todo_id="TODO-0001",
        pipeline_running=True,
    )
    return tmp_git_repo


class TestDashboardRenders:
    """DF5-7 regression: template must render without Jinja2 errors."""

    def test_template_syntax_valid(self, dash_project):
        """Dashboard template has no syntax errors (DF5-7: was {% endif %)."""
        result = generate_dashboard(dash_project)
        assert result is not None
        assert result.is_file()
        assert result.suffix == ".html"

    def test_html_contains_essential_sections(self, dash_project):
        """Generated HTML has key sections."""
        result = generate_dashboard(dash_project)
        html = result.read_text(encoding="utf-8")
        assert "DashTest" in html  # project name
        assert "agent-dev" in html  # stage name
        assert "TODO-0001" in html  # todo id

    def test_with_worker_activity(self, dash_project, monkeypatch):
        """Dashboard renders with worker_activity data present."""
        from awf.api import dashboard as dash_mod

        monkeypatch.setattr(
            dash_mod,
            "_read_worker_activity",
            lambda state: {"active": True, "state": "running", "pid": 12345, "cpu_seconds": 10, "worker_pid": 12345, "read_mb": 0, "write_kb": 0},
        )
        result = generate_dashboard(dash_project)
        assert result is not None
        html = result.read_text(encoding="utf-8")
        # v2 template: worker data passed to template even if section hidden by JS
        assert "worker" in html.lower()

    def test_without_worker_activity(self, dash_project):
        """Dashboard renders when worker_activity is absent (no crash)."""
        result = generate_dashboard(dash_project)
        assert result is not None

    def test_dashboard_overwrites_previous(self, dash_project):
        """Second call overwrites first (atomic write)."""
        generate_dashboard(dash_project)
        write_state(dash_project, stage_name="verify", stage_kind="verify", stage_idx=2)
        generate_dashboard(dash_project)
        result = dash_project / ".agentic" / "dashboards" / "current.html"
        html = result.read_text(encoding="utf-8")
        assert "verify" in html


class TestDashboardErrorHandling:
    """DF5-8: errors must be logged, not swallowed silently."""

    def test_missing_state_still_renders(self, tmp_git_repo):
        """No state file — dashboard still generates (uses defaults)."""
        api.init_project(tmp_git_repo, project_name="NoState")
        pipes_dir = tmp_git_repo / ".agentic" / "pipelines"
        pipes_dir.mkdir(parents=True, exist_ok=True)
        (pipes_dir / "default.yaml").write_text(
            "stages:\n  - name: plan\n    role: supervisor\n    kind: plan\n",
            encoding="utf-8",
        )
        # No write_state call
        result = generate_dashboard(tmp_git_repo)
        assert result is not None


class TestDashboardJinjaTemplate:
    """Direct template validation — catches syntax errors early."""

    def test_all_endif_have_closing_brace(self):
        """Every {% endif %} has closing } (DF5-7 was {% endif %)."""
        from awf.api.dashboard import _get_template

        template = _get_template()
        # If template loads, Jinja2 syntax is valid
        assert template is not None

    def test_template_renders_with_minimal_data(self):
        """Template renders with empty/minimal context."""
        from awf.api.dashboard import _get_template

        template = _get_template()
        html = template.render(
            project_name="Test",
            todo_id="",
            todo_summary="",
            elapsed="0s",
            status="idle",
            status_class="",
            status_text="Idle",
            status_icon="○",
            status_label="",
            stages=[],
            stages_done=0,
            stages_total=0,
            checkpoint_pending=False,
            checkpoint_form_url=None,
            events=[],
            handoffs=[],
            tasks=[],
            tasks_done=0,
            worker_activity=None,
        )
        assert len(html) > 0
        assert "<html" in html.lower() or "<!DOCTYPE" in html


class TestSalvageStatus:
    """dogfood-11: dashboard must show salvage instead of "Pipeline running".

    A worker that exits without a signal puts the orchestrator into salvage
    wait. The state file carried ``salvage_needed: true`` but the dashboard
    ignored it — users stared at a green "running" badge while the pipeline
    was stuck waiting for a supervisor decision.
    """

    def test_determine_status_salvage(self, dash_project):
        from awf.api.dashboard import _determine_status

        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        state = read_state(dash_project)
        status, _cls, text, _icon, label = _determine_status(state)
        assert status == "salvage"
        assert "agent-implementer" in text
        assert label == "Salvage"

    def test_state_dict_reports_salvage(self, dash_project):
        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        d = generate_state_dict(dash_project)
        assert d["status"] == "salvage"
        assert d["salvage_needed"] is True
        assert d["salvage_stage"] == "agent-implementer"

    def test_state_dict_reports_running_without_salvage(self, dash_project):
        d = generate_state_dict(dash_project)
        assert d["status"] == "running"
        assert d["salvage_needed"] is False
        assert d["salvage_stage"] is None

    def test_stage_start_write_clears_stale_salvage(self, dash_project):
        """Orchestrator's stage-start write must clear resolved salvage flags.

        ``write_state`` merges fields — without an explicit False/None the
        flag would stick forever after the first salvage.
        """
        write_state(dash_project, salvage_needed=True, salvage_stage="agent-implementer")
        write_state(
            dash_project,
            stage_name="agent-qa-review",
            salvage_needed=False,
            salvage_stage=None,
        )
        state = read_state(dash_project)
        assert state.get("salvage_needed") is False
        assert state.get("salvage_stage") is None


class TestTodoDiffStat:
    """Day-2 spec: diff-stat vs baseline in /api/state — one place for verify."""

    def test_diff_stat_reported(self, tmp_git_repo):
        import subprocess

        proj = tmp_git_repo
        (proj / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True,
        ).stdout.strip()
        (proj / ".agentic" / "context" / "BASELINE-TODO-0001.sha").write_text(
            sha + "\n", encoding="utf-8",
        )
        (proj / "README.md").write_text("changed by the stage\n", encoding="utf-8")
        write_state(
            proj, todo_id="TODO-0001", stage_name="agent-implementer", stage_kind="execute",
        )

        d = generate_state_dict(proj)

        assert "README.md" in d["todo_diff_stat"]

    def test_no_baseline_yields_empty(self, dash_project):
        d = generate_state_dict(dash_project)
        assert d["todo_diff_stat"] == ""


class TestHandoffChat:
    """Day-3 dashboard review: the chat shows ONLY the current TODO's handoffs."""

    def _write(self, proj, name, content="# H\n\nbody\n", mtime=None):
        import os

        d = proj / ".agentic" / "handoff"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text(content, encoding="utf-8")
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f

    def test_old_todo_handoffs_excluded(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0001.md")
        self._write(proj, "agent-implementer-TODO-0002.md")

        handoffs = _read_handoffs(
            proj, stage_order=["agent-implementer"], todo_id="TODO-0002",
        )

        assert [h["file"] for h in handoffs] == ["agent-implementer-TODO-0002.md"]

    def test_legacy_final_variant_included(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0002-final.md")

        handoffs = _read_handoffs(proj, todo_id="TODO-0002")

        assert len(handoffs) == 1
        assert handoffs[0]["role"] == "agent-qa-review"

    def test_idless_files_ignored(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-implementer.md")  # legacy convention

        assert _read_handoffs(proj, todo_id="TODO-0002") == []

    def test_no_todo_id_returns_empty(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-x-TODO-0001.md")

        assert _read_handoffs(proj, todo_id=None) == []

    def test_dedupe_prefers_canonical_name(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-qa-review-TODO-0002-final.md", mtime=2_000_000)
        self._write(proj, "agent-qa-review-TODO-0002.md", mtime=1_000_000)

        handoffs = _read_handoffs(proj, todo_id="TODO-0002")

        assert len(handoffs) == 1
        assert handoffs[0]["file"] == "agent-qa-review-TODO-0002.md"

    def test_rev_changes_with_content(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        f = self._write(proj, "agent-x-TODO-0002.md")
        rev1 = _read_handoffs(proj, todo_id="TODO-0002")[0]["rev"]

        f.write_text("# H\n\nnew body entirely\n", encoding="utf-8")
        rev2 = _read_handoffs(proj, todo_id="TODO-0002")[0]["rev"]

        assert rev1 != rev2

    def test_raw_html_escaped(self, tmp_git_repo):
        from awf.api.dashboard import _read_handoffs

        proj = tmp_git_repo
        self._write(proj, "agent-x-TODO-0002.md", content="# T\n\n<script>alert(1)</script>\n")

        html = _read_handoffs(proj, todo_id="TODO-0002")[0]["content_html"]

        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestContentEscaping:
    """Day-3: LLM-written content must not inject markup into the dashboard."""

    def test_todo_raw_html_escaped(self, dash_project):
        from awf.api.dashboard import _read_todo_content

        inbox = dash_project / ".agentic" / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "TODO-0001.md").write_text(
            "# T\n\n<script>alert(1)</script>\n", encoding="utf-8",
        )

        html = _read_todo_content(dash_project, "TODO-0001")

        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_script_tag_escaped_in_embedded_state(self, dash_project):
        from awf import run_state

        proj = dash_project
        run_state.write_run(
            proj, active=True, queue=["TODO-0001"], index=1,
            current="evil</script><script>alert(1)</script>",
        )

        out = generate_dashboard(proj)
        html = out.read_text(encoding="utf-8")

        assert "</script><script>alert(1)" not in html
        assert "\\u003c/script" in html


class TestTimelineCommitShas:
    def test_verify_commit_sha_in_timeline(self, dash_project):
        import subprocess

        from awf.api.dashboard import _build_todo_timeline

        proj = dash_project
        (proj / "x.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "awf(verify): TODO-0001"], cwd=proj, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=proj,
            capture_output=True, text=True,
        ).stdout.strip()
        (proj / ".agentic" / "done" / "TODO-0001").mkdir(parents=True)

        timeline = _build_todo_timeline(proj, None)

        assert timeline[0]["id"] == "TODO-0001"
        assert timeline[0]["commit_sha"] == sha


class TestDashboardPort:
    def test_second_server_binds_same_port(self, tmp_git_repo):
        from awf.api.dashboard_server import start_dashboard_server

        port1, server1 = start_dashboard_server(tmp_git_repo)
        server1.shutdown()
        server1.server_close()

        port2, server2 = start_dashboard_server(tmp_git_repo, port=port1)
        try:
            assert port2 == port1
        finally:
            server2.shutdown()
            server2.server_close()

    def test_busy_port_raises(self, tmp_git_repo):
        from awf.api.dashboard_server import start_dashboard_server

        port1, server1 = start_dashboard_server(tmp_git_repo)
        try:
            with pytest.raises(OSError):
                start_dashboard_server(tmp_git_repo, port=port1)
        finally:
            server1.shutdown()
            server1.server_close()
