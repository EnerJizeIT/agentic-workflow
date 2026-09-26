"""A-16 (audit 2026-09-25, layer 6): the project-setup DOM is built without HTML from data.

Defect: the template concatenated external strings (model/role/skill names,
skill content from local SKILL.md files) into HTML string fragments
(`innerHTML`), escaping only `"` — a hostile skill title executed script in
the opened form. Fix: elements are created via the DOM API
(`createElement`/`textContent`/`value`), data reaches the page as JSON
literals (`tojson`) and no HTML string is assembled from it in JS.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from agent_workflow_ui.render.engine import create_default_env, render_template

import agent_workflow_ui as _awui

TEMPLATE_PATH = (
    Path(_awui.__file__).parent / "render" / "default_templates" / "project-setup.html.j2"
)

PAYLOAD = "<img src=x onerror=alert(1)>"


def _render(**overrides) -> str:
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


def test_no_innerhtml_from_data_in_project_setup_template():
    """A-16: no innerHTML/outerHTML/insertAdjacentHTML in the template,
    skill data displayed via textContent, and a hostile skill title survives
    rendering only as an inert JSON literal (tojson) — no raw HTML."""
    content = TEMPLATE_PATH.read_text(encoding="utf-8")

    # 1) Static: no HTML-injection point for data exists in the template.
    for marker in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        assert marker not in content, f"template still contains {marker}"

    # Skill data (title/content) must be assigned as text, not markup.
    assert "textContent" in content, "skill data must be assigned via textContent"

    # 2) Render with a hostile skill: data must reach the page only inside
    # the globalSkills JSON literal — nowhere else in the document.
    rendered = _render(
        global_skills=[
            {"id": "evil", "title": PAYLOAD, "description": "d", "content": PAYLOAD},
        ]
    )
    assert PAYLOAD not in rendered, "raw hostile payload leaked into the HTML document"

    script = rendered.split("<script>", 1)[1].split("</script>", 1)[0]
    skill_line = next(
        line for line in script.splitlines() if line.strip().startswith('{id: "evil"')
    )
    title_literal = re.search(r'title: ("(?:[^"\\]|\\.)*")', skill_line).group(1)
    assert json.loads(title_literal) == PAYLOAD, (
        "hostile title must round-trip as data through the JSON literal"
    )

    # 3) Regressions: the form still renders with and without data.
    assert _render()
    assert _render(global_skills=[{"id": "ok", "title": "t", "content": "c"}])
