"""Tests for Jinja2 render layer."""
from __future__ import annotations

import pytest
from agent_workflow_ui.render.engine import create_env, render_template
from agent_workflow_ui.render.frontmatter import (
    parse_frontmatter,
    parse_frontmatter_from_file,
)


def test_parse_frontmatter_basic():
    content = """---
description: Test
required_data_keys:
  - foo
---
<body>{{ foo }}</body>
"""
    meta, body = parse_frontmatter(content)
    assert meta["description"] == "Test"
    assert meta["required_data_keys"] == ["foo"]
    assert "<body>" in body


def test_parse_frontmatter_no_frontmatter():
    content = "<h1>Plain template</h1>"
    meta, body = parse_frontmatter(content)
    assert meta == {}
    assert body == content


def test_parse_frontmatter_malformed_returns_empty():
    """Malformed YAML frontmatter doesn't raise."""
    content = """---
not: valid: yaml: at: all
---
<body>foo</body>
"""
    meta, body = parse_frontmatter(content)
    assert meta == {}
    assert "<body>" in body


def test_parse_frontmatter_from_file(tmp_path):
    f = tmp_path / "test.html.j2"
    f.write_text("""---
description: From file
---
<body>{{ name }}</body>
""")
    meta, body = parse_frontmatter_from_file(f)
    assert meta["description"] == "From file"


def test_create_env_and_render(tmp_path):
    """Render basic template."""
    (tmp_path / "greeting.html.j2").write_text("<h1>Hello {{ name }}</h1>")
    env = create_env([tmp_path])
    result = render_template(env, "greeting", {"name": "World"})
    assert result == "<h1>Hello World</h1>"


def test_create_env_multiple_dirs_project_overrides_default(tmp_path):
    """Project templates override default by name."""
    default_dir = tmp_path / "default"
    project_dir = tmp_path / "project"
    default_dir.mkdir()
    project_dir.mkdir()

    (default_dir / "role.html.j2").write_text("<p>default {{ x }}</p>")
    (project_dir / "role.html.j2").write_text("<p>project {{ x }}</p>")

    env = create_env([project_dir, default_dir])
    result = render_template(env, "role", {"x": "value"})
    assert "project" in result
    assert "default" not in result


def test_render_template_autoescape(tmp_path):
    """HTML autoescape is on by default."""
    (tmp_path / "x.html.j2").write_text("<p>{{ user_input }}</p>")
    env = create_env([tmp_path])
    result = render_template(env, "x", {"user_input": "<script>alert(1)</script>"})
    assert "<script>" not in result
    assert "&lt;script&gt;" in result


def test_render_template_injects_template_name(tmp_path):
    """template_name is available to all templates."""
    (tmp_path / "meta.html.j2").write_text("template={{ template_name }}")
    env = create_env([tmp_path])
    result = render_template(env, "meta", {})
    assert "template=meta" in result


def test_render_template_missing_raises(tmp_path):
    """Missing template raises TemplateNotFound."""
    env = create_env([tmp_path])
    from jinja2 import TemplateNotFound
    with pytest.raises(TemplateNotFound):
        render_template(env, "does-not-exist", {})


def test_create_env_empty_dirs_raises():
    """create_env with empty list raises ValueError."""
    with pytest.raises(ValueError, match="must not be empty"):
        create_env([])


def test_parse_frontmatter_non_dict_yaml():
    """Frontmatter that parses to non-dict returns empty metadata."""
    content = """---
- just a list
- not a dict
---
<body>foo</body>
"""
    meta, body = parse_frontmatter(content)
    assert meta == {}
    assert "<body>" in body
