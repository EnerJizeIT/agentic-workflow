"""Tests for Jinja2 render layer."""
from __future__ import annotations

import html
import json
import re

import pytest
from agent_workflow_ui.render.engine import create_default_env, create_env, render_template
from agent_workflow_ui.render.frontmatter import (
    parse_frontmatter,
    parse_frontmatter_from_file,
)

# ── FU-16 / AUD09-02: JS-context injection in onclick attribute ──────────


def _render_increment_planning(variants: list[dict]) -> str:
    env = create_default_env()
    return render_template(env, "increment-planning", {
        "variants": variants,
        "form_id": "FORM-test",
        "submit_url": "http://127.0.0.1:1/submit/FORM-test",
    })


def test_increment_planning_onclick_normal_id():
    """Sanity: a plain id renders as a single JS string literal."""
    rendered = _render_increment_planning([{"id": "A", "title": "t", "description": "d"}])
    assert "selectVariant(&quot;A&quot;)" in rendered or 'selectVariant("A")' in rendered


def test_increment_planning_onclick_js_context():
    """AUD09-02 XSS: the browser decodes HTML entities in attribute
    values BEFORE running the JS, so plain autoescape (&#39;) is not
    enough — a quote in v.id broke out of the selectVariant literal.

    The rendered argument must be a single JS string literal: no raw
    double quote inside it (cannot end the literal), no raw single
    quote anywhere in the attribute (cannot end the attribute).
    """
    payload = "A'); console.log('PWNED'); //"
    rendered = _render_increment_planning([{"id": payload, "title": "t", "description": "d"}])

    m = re.search(r"onclick=['\"]selectVariant\((.*)\)['\"]", rendered)
    assert m, "onclick handler not found in rendered form"
    raw_arg = m.group(1)

    # No raw single quote in the attribute — it would close a
    # single-quoted attribute (tojson escapes ' as \u0027).
    assert "'" not in raw_arg, f"raw single quote in onclick attribute: {raw_arg!r}"

    arg = html.unescape(raw_arg)
    assert arg.startswith('"') and arg.endswith('"'), (
        f"onclick arg is not a single double-quoted JS string: {arg!r}"
    )
    inner = arg[1:-1]
    assert '"' not in inner, (
        f"raw double quote inside the JS literal — injection possible: {arg!r}"
    )
    # The payload survives intact as inert data.
    assert payload.replace("'", "\\u0027") in arg


# ── FU-16 / AUD09-03: hostile role data must not break <script> ──────────


def _render_project_setup(**overrides) -> str:
    """Render project-setup with the standard plugin context, overrides win."""
    env = create_default_env()
    context = {
        "form_id": "FORM-test",
        "submit_url": "http://127.0.0.1:1/submit/FORM-test",
        "available_roles": [{"id": "worker", "title": "worker", "description": ""}],
        "custom_supervisor_roles": [],
        "custom_agents": [{"id": "qa", "title": "qa"}],
        "global_skills": [],
        "available_models": ["acme/m1"],
        "recent_models": [],
        "project_roles": [{"id": "local", "title": "local"}],
        "existing_supervisor_slugs": ["sup"],
        "existing_agent_slugs": ["ag"],
    }
    context.update(overrides)
    return render_template(env, "project-setup", context)


def _script_array(rendered: str, var: str) -> list:
    """Extract `var <name> = ...;` from the form's <script> and parse it.

    The array must be a single-line JSON literal (tojson). Before the fix
    the arrays were multi-line raw interpolation — not parseable at all.
    """
    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    for line in script.splitlines():
        if line.strip().startswith(f"var {var} ="):
            literal = line.strip()[len(f"var {var} ="):].rstrip(";").strip()
            return json.loads(literal)
    pytest.fail(f"var {var} = not found as a single-line assignment")


def test_project_setup_script_survives_backslash_role():
    """AUD09-03: backslash / newline / quote in role data must not produce
    a JS SyntaxError (repro: id `qa\\` broke the whole IIFE). Every
    array must round-trip through json.loads with the data intact."""
    rendered = _render_project_setup(
        available_roles=[
            {"id": "qa\\", "title": "qa\\", "description": "use C:\\temp\\ paths\nand newlines"},
        ],
        custom_agents=[{"id": "ca\\", "title": "ca\\"}],
        project_roles=[{"id": "pr\\", "title": "pr\\"}],
        available_models=["acme/m\\odel"],
        recent_models=["acme/r\nmodel"],
        existing_supervisor_slugs=["sup\\"],
        existing_agent_slugs=["ag\\"],
    )

    assert _script_array(rendered, "roles")[0]["id"] == "qa\\"
    assert _script_array(rendered, "roles")[0]["description"] == "use C:\\temp\\ paths\nand newlines"
    assert _script_array(rendered, "customAgents")[0]["id"] == "ca\\"
    assert _script_array(rendered, "projectRoles")[0]["id"] == "pr\\"
    assert _script_array(rendered, "allModels") == ["acme/m\\odel"]
    assert _script_array(rendered, "recentModels") == ["acme/r\nmodel"]
    assert _script_array(rendered, "existingSupervisorSlugs") == ["sup\\"]
    assert _script_array(rendered, "existingAgentSlugs") == ["ag\\"]


def test_project_setup_script_ordinary_data_unchanged():
    """Sanity: ordinary ids still render into the arrays (tojson form)."""
    rendered = _render_project_setup()
    assert _script_array(rendered, "roles")[0]["id"] == "worker"
    assert _script_array(rendered, "customAgents")[0]["id"] == "qa"
    assert _script_array(rendered, "allModels") == ["acme/m1"]


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
