"""Unit tests for awf.api.planning — increment plan persistence."""
from __future__ import annotations

import pytest
import yaml

from awf import api


@pytest.fixture
def awf_project(tmp_git_repo):
    """Project with .agentic/ initialized (plan.md is a stub)."""
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


class TestApplyIncrementPlan:
    def test_writes_plan_md(self, awf_project):
        """Selected variant persisted to .agentic/phases/plan.md."""
        plan_md = """# Plan: vertical slice

Strategy: vertical

## Increments

1. I1: storyboard only
2. I2: + Jira
"""
        result = api.apply_increment_plan(
            awf_project,
            plan_md,
            selected_variant_id="A",
            variants=[
                {"id": "A", "title": "vertical"},
                {"id": "B", "title": "horizontal"},
            ],
        )
        assert isinstance(result, api.ApplyIncrementPlanResult)
        assert result.selected_variant_id == "A"
        assert result.variants_offered == 2
        assert ".agentic/phases/plan.md" in result.plan_path

        # File written with frontmatter + body
        content = (awf_project / ".agentic" / "phases" / "plan.md").read_text()
        assert "selected_variant: A" in content
        assert "variants_offered: 2" in content
        assert "vertical slice" in content
        assert "I1: storyboard only" in content

    def test_without_variants_still_works(self, awf_project):
        """Supervisor can persist plan without variants list (verbal offer)."""
        result = api.apply_increment_plan(
            awf_project,
            "# Simple plan\n\nJust do it.",
        )
        assert result.selected_variant_id is None
        assert result.variants_offered is None

        content = (awf_project / ".agentic" / "phases" / "plan.md").read_text()
        assert "Just do it" in content
        # No selected_variant frontmatter
        assert "selected_variant" not in content

    def test_replaces_existing_plan_md(self, awf_project):
        """Re-submitting replaces previous plan (idempotent overwrite)."""
        api.apply_increment_plan(awf_project, "# Plan v1")
        api.apply_increment_plan(awf_project, "# Plan v2")
        content = (awf_project / ".agentic" / "phases" / "plan.md").read_text()
        assert "Plan v2" in content
        assert "Plan v1" not in content

    def test_empty_plan_rejected(self, awf_project):
        with pytest.raises(api.AwfApiError, match="plan_markdown is required"):
            api.apply_increment_plan(awf_project, "")
        with pytest.raises(api.AwfApiError, match="plan_markdown is required"):
            api.apply_increment_plan(awf_project, "   \n  ")

    def test_missing_agentic_raises(self, tmp_git_repo):
        with pytest.raises(api.AwfApiError, match="No .agentic/"):
            api.apply_increment_plan(tmp_git_repo, "# Plan")

    def test_frontmatter_yaml_valid(self, awf_project):
        """Frontmatter must be valid YAML for parsers."""
        api.apply_increment_plan(
            awf_project,
            "# Plan",
            selected_variant_id="B",
            variants=[{"id": "A"}, {"id": "B"}],
        )
        content = (awf_project / ".agentic" / "phases" / "plan.md").read_text()
        # Extract frontmatter between --- markers
        lines = content.splitlines()
        if lines[0] == "---":
            end = next(
                (i for i, ln in enumerate(lines[1:], start=1) if ln == "---"),
                None,
            )
            if end:
                fm = "\n".join(lines[1:end])
                data = yaml.safe_load(fm)
                assert isinstance(data, dict)
                assert data["selected_variant"] == "B"
                assert data["variants_offered"] == 2

    def test_as_dict_serializable(self, awf_project):
        import json
        result = api.apply_increment_plan(awf_project, "# Plan", selected_variant_id="A")
        d = result.as_dict()
        json.dumps(d)  # must not raise


class TestBuildPlanMdFromVariant:
    """Test the helper in http_endpoint that converts variant → plan.md body."""

    def test_renders_full_variant(self):
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        selected = {
            "id": "A",
            "title": "Vertical slice",
            "strategy": "vertical",
            "description": "Each step delivers user value.",
            "estimated_todos": 3,
            "estimated_time": "1 week",
            "increments": [
                {"name": "I1: storyboard", "goal": "Generate HTML from JSON", "artefacts": ["storyboard.js"]},
                {"name": "I2: + Jira", "goal": "Fetch epic from Jira", "artefacts": ["jira-client.js"]},
            ],
            "pros": ["Fast feedback"],
            "cons": ["Refactoring overhead"],
        }
        body = _build_plan_md_from_variant(selected, [selected])
        assert "Vertical slice" in body
        assert "vertical" in body
        assert "I1: storyboard" in body
        assert "I2: + Jira" in body
        assert "storyboard.js" in body
        assert "Fast feedback" in body
        assert "Refactoring overhead" in body
        assert "~3 TODOs" in body
        assert "~1 week" in body

    def test_renders_other_variants_as_history(self):
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        selected = {"id": "A", "title": "Picked"}
        others = [
            {"id": "A", "title": "Picked"},
            {"id": "B", "title": "Alternative 1"},
            {"id": "C", "title": "Alternative 2"},
        ]
        body = _build_plan_md_from_variant(selected, others)
        assert "Other variants considered" in body
        assert "Alternative 1" in body
        assert "Alternative 2" in body

    def test_minimal_variant(self):
        """Variant with only id+title still renders."""
        from agent_workflow_ui.http_endpoint import _build_plan_md_from_variant

        body = _build_plan_md_from_variant(
            {"id": "X", "title": "Minimal"},
            [{"id": "X", "title": "Minimal"}],
        )
        assert "Minimal" in body
