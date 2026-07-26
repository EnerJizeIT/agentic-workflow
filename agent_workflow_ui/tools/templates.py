"""MCP tool: list available templates."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..state import get_config
from ..render.frontmatter import parse_frontmatter_from_file


async def list_templates() -> dict[str, Any]:
    """List all available form templates (default + project-level).

    Returns:
        Dict with templates (list of {name, source, description, required_data_keys, optional_data_keys}).
    """
    config = get_config()
    default_dir = Path(__file__).resolve().parents[1] / "render" / "default_templates"

    templates: dict[str, dict] = {}

    if default_dir.is_dir():
        for path in sorted(default_dir.glob("*.html.j2")):
            name = path.name.removesuffix(".html.j2")
            try:
                meta, _ = parse_frontmatter_from_file(path)
                templates[name] = {
                    "name": name,
                    "source": "default",
                    "description": meta.get("description", ""),
                    "required_data_keys": meta.get("required_data_keys", []),
                    "optional_data_keys": meta.get("optional_data_keys", []),
                }
            except Exception:
                continue

    if config.templates_dir.is_dir():
        for path in sorted(config.templates_dir.glob("*.html.j2")):
            name = path.name.removesuffix(".html.j2")
            try:
                meta, _ = parse_frontmatter_from_file(path)
                templates[name] = {
                    "name": name,
                    "source": "project",
                    "description": meta.get("description", ""),
                    "required_data_keys": meta.get("required_data_keys", []),
                    "optional_data_keys": meta.get("optional_data_keys", []),
                }
            except Exception:
                continue

    return {
        "templates": list(templates.values()),
    }
