"""YAML frontmatter parser for .html.j2 files.

Format:
    ---
    description: Template description
    required_data_keys:
      - key1
    optional_data_keys:
      - key2
    ---
    <template body>
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter from .html.j2 file content.

    Args:
        content: Full file content as string.

    Returns:
        Tuple (metadata, body). If no frontmatter, metadata is {} and body is content unchanged.
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    frontmatter_str, body = match.groups()
    try:
        metadata = yaml.safe_load(frontmatter_str)
    except yaml.YAMLError:
        return {}, content
    if not isinstance(metadata, dict):
        return {}, content
    return metadata, body


def parse_frontmatter_from_file(path: Path) -> tuple[dict[str, Any], str]:
    """Read file and parse frontmatter.

    Args:
        path: Path-like to .html.j2 file.

    Returns:
        Tuple (metadata, body).
    """
    content = path.read_text(encoding="utf-8")
    return parse_frontmatter(content)
