"""MCP tool: list available templates."""
from __future__ import annotations

from typing import Any


async def list_templates() -> dict[str, Any]:
    """List all available form templates (default + project-level).

    Returns:
        Dict with templates (list of {name, source, description, ...}).
    """
    return {
        "templates": [],
        "error": "Not implemented yet (Epic 4). No templates registered.",
    }
