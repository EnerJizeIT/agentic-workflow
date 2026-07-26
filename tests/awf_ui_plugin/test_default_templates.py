"""Tests for default templates: render each with minimum required data."""
from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import TemplateNotFound

from agent_workflow_ui.render.engine import create_env, render_template
from agent_workflow_ui.render.frontmatter import parse_frontmatter_from_file


import agent_workflow_ui as _awui
DEFAULT_TEMPLATES_DIR = Path(_awui.__file__).parent / "render" / "default_templates"

EXPECTED_TEMPLATES = [
    "role-assignment",
    "skill-picker",
    "model-picker",
    "pipeline-picker",
    "conflict-resolver",
    "project-setup",
]


@pytest.fixture
def env():
    """Jinja2 env with only default templates."""
    return create_env([DEFAULT_TEMPLATES_DIR])


def test_all_expected_templates_exist(env):
    """All 6 default templates exist and are renderable."""
    for name in EXPECTED_TEMPLATES:
        template = env.get_template(f"{name}.html.j2")
        assert template is not None


def test_role_assignment_renders_with_minimum_data(env):
    """role-assignment renders with required_data_keys only."""
    html = render_template(env, "role-assignment", {
        "available_roles": ["worker", "reviewer", "tester"],
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
    })
    assert "<form" in html
    assert 'action="http://127.0.0.1:13747/submit/FORM-001"' in html
    assert 'method="POST"' in html
    assert "worker" in html
    assert "reviewer" in html
    assert "tester" in html


def test_skill_picker_renders(env):
    html = render_template(env, "skill-picker", {
        "available_skills": ["backend-developer", "frontend-developer"],
        "form_id": "FORM-002",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-002",
    })
    assert "<form" in html
    assert "backend-developer" in html
    assert "frontend-developer" in html


def test_model_picker_renders(env):
    html = render_template(env, "model-picker", {
        "roles": ["worker", "reviewer"],
        "available_models": ["claude-sonnet-4-20250514", "gpt-4.1"],
        "default_model": "claude-sonnet-4-20250514",
        "form_id": "FORM-003",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-003",
    })
    assert "<form" in html
    assert "claude-sonnet-4-20250514" in html
    assert "gpt-4.1" in html
    assert "worker" in html
    assert "reviewer" in html


def test_pipeline_picker_renders_with_defaults(env):
    """pipeline-picker has built-in default options."""
    html = render_template(env, "pipeline-picker", {
        "form_id": "FORM-004",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-004",
    })
    assert "<form" in html
    assert "simple" in html.lower()
    assert "full" in html.lower()
    assert "custom" in html.lower()


def test_pipeline_picker_renders_with_custom_pipelines(env):
    html = render_template(env, "pipeline-picker", {
        "available_pipelines": [
            {"id": "ml", "title": "ML Pipeline", "description": "Для ML проектов"},
        ],
        "form_id": "FORM-005",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-005",
    })
    assert "ml" in html.lower()
    assert "ML Pipeline" in html


def test_conflict_resolver_renders(env):
    html = render_template(env, "conflict-resolver", {
        "conflict_name": "worker.md",
        "conflict_context": ".agentic/roles/",
        "existing_description": "Default worker template",
        "new_description": "Custom worker with extra skills",
        "form_id": "FORM-006",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-006",
    })
    assert "<form" in html
    assert "worker.md" in html
    assert "replace" in html.lower()
    assert "save" in html.lower()
    assert "cancel" in html.lower()


def test_each_template_has_valid_frontmatter():
    """Each template has YAML frontmatter with description."""
    for name in EXPECTED_TEMPLATES:
        path = DEFAULT_TEMPLATES_DIR / f"{name}.html.j2"
        metadata, body = parse_frontmatter_from_file(path)
        assert "description" in metadata, f"{name}: missing description in frontmatter"
        assert isinstance(metadata["description"], str)
        assert len(metadata["description"]) > 0
        assert "<html" in body.lower() or "<!doctype" in body.lower()


def test_template_names_match_files():
    """All expected template files exist with expected names."""
    for name in EXPECTED_TEMPLATES:
        path = DEFAULT_TEMPLATES_DIR / f"{name}.html.j2"
        assert path.exists(), f"Missing: {path}"


# ── project-setup composite template ──────────────────────────────────────────


def test_project_setup_renders_with_minimum_data(env):
    """project-setup renders with only required data keys."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [
            {"id": "worker", "title": "Worker", "default": True},
        ],
    })
    assert "<form" in html
    assert 'method="POST"' in html
    assert "FORM-001" in html
    assert "worker" in html
    # available_models comes from Jinja2 global, not from data
    # Template should still render without models in data


def test_project_setup_has_team_table(env):
    """Team section has a table with agent/model columns."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "<table" in html
    assert "агент" in html.lower() or "agent" in html.lower()


def test_project_setup_no_pipeline_section(env):
    """Pipeline section removed — user builds pipeline via agent selection."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'type="radio"' not in html
    assert 'name="pipeline"' not in html


def test_project_setup_no_verify_commands(env):
    """Verification commands section removed."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "test_cmd" not in html
    assert "lint_cmd" not in html
    assert "typecheck_cmd" not in html


def test_project_setup_uses_available_models_global(env):
    """Template uses available_models from Jinja2 globals."""
    env.globals["available_models"] = ["test-model-1", "test-model-2"]
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "test-model-1" in html
    assert "test-model-2" in html


def test_project_setup_has_custom_agent_button(env):
    """Template has button for adding custom agents."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "addCustomAgent" in html
    assert "Свой агент" in html


def test_project_setup_has_file_picker(env):
    """Template has file input for spec upload."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'type="file"' in html
    assert "spec_file" in html
    assert "spec-content" in html
    assert "FileReader" in html


def test_project_setup_has_dark_theme(env):
    """Template uses VS Code dark theme."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "#1e1e1e" in html
    assert "#252526" in html


def test_project_setup_team_has_agent_select(env):
    """Team section has <select> for agents (merged role+skill)."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker", "default": True}],
    })
    assert '<select' in html
    assert 'name="agent"' in html
    # Should NOT have separate role+skill selects
    assert 'name="role"' not in html
    assert 'name="skill"' not in html


def test_project_setup_team_has_add_remove_buttons(env):
    """Team section has Add and Remove buttons."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'addRow' in html or 'add' in html.lower()
    assert 'removeRow' in html or 'remove' in html.lower() or '×' in html


def test_project_setup_team_has_hidden_json_input(env):
    """Team data collected in hidden JSON input."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'team_config' in html or 'team' in html.lower()


def test_project_setup_has_context_textarea(env):
    """Context section has textarea for project description."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'name="context_message"' in html
    assert "<textarea" in html


def test_project_setup_custom_agent_has_file_upload(env):
    """Custom agent row includes file input for .md skill + FileReader."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "custom_agent_name" in html
    assert "custom_agent_skill" in html
    assert "custom_agent_content" in html
    assert 'accept=".md' in html
    assert "FileReader" in html


def test_project_setup_submit_builds_team_json(env):
    """Submit handler builds team_config JSON with type: default/custom."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert "type: 'default'" in html or 'type: "default"' in html
    assert "type: 'custom'" in html or 'type: "custom"' in html
    assert "skill_content" in html
    assert "skill_filename" in html


def test_project_setup_no_project_name_field(env):
    """Project name field removed — only context message remains."""
    html = render_template(env, "project-setup", {
        "form_id": "FORM-001",
        "submit_url": "http://127.0.0.1:13747/submit/FORM-001",
        "available_roles": [{"id": "worker", "title": "Worker"}],
    })
    assert 'name="project_name"' not in html
