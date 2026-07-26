"""Jinja2 rendering engine with template discovery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape


def create_env(templates_dirs: list[Path]) -> Environment:
    """Create Jinja2 environment.

    Templates are searched in order: first entry wins (project templates override defaults).
    Typically templates_dirs = [project_dir, plugin_defaults_dir].
    """
    if not templates_dirs:
        raise ValueError("templates_dirs must not be empty")

    loader = ChoiceLoader([
        FileSystemLoader(str(d)) for d in templates_dirs
    ])
    env = Environment(
        loader=loader,
        autoescape=select_autoescape(["html", "htm", "html.j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    return env


def render_template(
    env: Environment,
    template_name: str,
    context: dict[str, Any],
) -> str:
    """Render a template by name.

    Args:
        env: Jinja2 environment.
        template_name: Template name without extension (e.g., "role-assignment").
        context: Variables to pass to template. Plugin injects:
            - form_id (str)
            - submit_url (str)
            - template_name (str)

    Returns:
        Rendered HTML string.
    """
    template_file = f"{template_name}.html.j2"
    template = env.get_template(template_file)
    full_context = {
        "template_name": template_name,
        **context,
    }
    return template.render(**full_context)
