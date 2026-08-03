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


# П2/П6: candidate filenames in priority order. The numbered prefix on
# PRODUCT-VISION matches the convention used in user's pet-projects
# (forces sort order at top of file listing).
_VISION_CANDIDATES = (
    "1. PRODUCT-VISION.md",
    "PRODUCT-VISION.md",
    "VISION.md",
    "vision.md",
    "README.md",
    "README.MD",
    "readme.md",
)


def find_vision_file(project_dir: str | Path = ".") -> Path | None:
    """П2/П6: locate the project's high-level vision/context document.

    Searches project root (NOT subdirs — keeps it fast and avoids noise
    from node_modules/README, etc).

    Returns the first match from ``_VISION_CANDIDATES`` or ``None``.
    Callers (cmd_init for plan.md stub, supervisor.build_prompt for
    context injection) use this to give LLM-supervisor a starting point
    instead of an empty plan.md / generic prompt.
    """
    root = Path(project_dir).resolve()
    for name in _VISION_CANDIDATES:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None

