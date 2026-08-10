"""Jinja2 rendering engine with template discovery."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader

from .frontmatter import parse_frontmatter


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
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    return env


def create_default_env() -> Environment:
    """A3: create a default Jinja2 env loading only plugin's default_templates.

    Used by lazy initialization in state.get_jinja_env() so one-off
    renders (like _ack_page from http_endpoint) work without explicit
    set_jinja_env() at startup.
    """
    default_templates = Path(__file__).parent / "default_templates"
    return create_env([default_templates])


def render_template(
    env: Environment,
    template_name: str,
    context: dict[str, Any],
) -> str:
    """Render a template by name, stripping YAML frontmatter from output.

    Args:
        env: Jinja2 environment.
        template_name: Template name without extension (e.g., "project-setup").
        context: Variables to pass to template. Plugin injects:
            - form_id (str)
            - submit_url (str)
            - template_name (str)

    Returns:
        Rendered HTML string (without frontmatter).
    """
    template_file = f"{template_name}.html.j2"
    source, _, _ = env.loader.get_source(env, template_file)
    _, body = parse_frontmatter(source)
    template = env.from_string(body)
    full_context = {
        "template_name": template_name,
        **context,
    }
    return template.render(**full_context)
