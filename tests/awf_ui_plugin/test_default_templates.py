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
]


@pytest.fixture
def env():
    """Jinja2 env with only default templates."""
    return create_env([DEFAULT_TEMPLATES_DIR])


def test_all_expected_templates_exist(env):
    """All 5 default templates exist and are renderable."""
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
    """5 template files exist with expected names."""
    for name in EXPECTED_TEMPLATES:
        path = DEFAULT_TEMPLATES_DIR / f"{name}.html.j2"
        assert path.exists(), f"Missing: {path}"
