"""Path resolution for .agentic/ directories."""
from __future__ import annotations

from pathlib import Path


def agentic_dir(project_dir: str | Path = ".") -> Path:
    """Return the .agentic/ directory for the given project."""
    return Path(project_dir).resolve() / ".agentic"


def inbox(project_dir: str | Path = ".") -> Path:
    """Return .agentic/inbox/."""
    return agentic_dir(project_dir) / "inbox"


def outbox(project_dir: str | Path = ".") -> Path:
    """Return .agentic/outbox/."""
    return agentic_dir(project_dir) / "outbox"


def context_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/context/."""
    return agentic_dir(project_dir) / "context"


def config_file(project_dir: str | Path = ".") -> Path:
    """Return .agentic/config.yaml."""
    return agentic_dir(project_dir) / "config.yaml"


def skills_dir(project_dir: str | Path = ".") -> Path:
    """Return .agentic/skills/."""
    return agentic_dir(project_dir) / "skills"
