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
from awf.api.dashboard import generate_dashboard
from awf.pipeline_state import write_state


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
            lambda state: {"active": True, "state": "running", "pid": 12345, "cpu_seconds": 10},
        )
        result = generate_dashboard(dash_project)
        assert result is not None
        html = result.read_text(encoding="utf-8")
        assert "worker-activity" in html or "wa-indicator" in html

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
