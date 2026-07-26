"""Auto-install SKILL.md into opencode skills directory on plugin startup.

This runs at every plugin start (every opencode session). It's idempotent:
- If skill file doesn't exist → copy.
- If skill file exists but content differs → overwrite (keeps skill in sync
  with installed plugin version after `pip install --upgrade`).
- If skill file exists and is identical → no-op.

This replaces fragile setuptools post-install hooks (which don't work reliably
for user-locale paths like ~/.config/) with a self-healing lazy install.
The end-user principle: `pip install agent-workflow-ui` + add MCP config to
opencode.json — that's it. Plugin handles the rest on first run.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
SKILLS_DIR_NAME = "agent-workflow-ui"


def _bundled_skill_path() -> Path:
    """Path to SKILL.md bundled inside the installed package."""
    return Path(__file__).resolve().parent / SKILL_FILENAME


def _target_skill_path() -> Path:
    """Path where opencode expects the skill: ~/.config/opencode/skills/<name>/SKILL.md."""
    return Path.home() / ".config" / "opencode" / "skills" / SKILLS_DIR_NAME / SKILL_FILENAME


def ensure_skill_installed() -> bool:
    """Ensure SKILL.md is installed in opencode skills directory.

    Returns:
        True if skill is in place (copied or already current), False on failure.
    """
    bundled = _bundled_skill_path()
    target = _target_skill_path()

    if not bundled.exists():
        log.warning("Bundled SKILL.md not found at %s — skipping install", bundled)
        return False

    try:
        bundled_content = bundled.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Failed to read bundled SKILL.md: %s", e)
        return False

    # Check if already up-to-date (idempotent fast path)
    if target.exists():
        try:
            if target.read_text(encoding="utf-8") == bundled_content:
                return True
        except OSError:
            pass  # fall through to overwrite

    # Copy / overwrite
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundled_content, encoding="utf-8")
        log.info("Installed SKILL.md → %s", target)
        return True
    except OSError as e:
        log.error("Failed to install SKILL.md to %s: %s", target, e)
        return False
