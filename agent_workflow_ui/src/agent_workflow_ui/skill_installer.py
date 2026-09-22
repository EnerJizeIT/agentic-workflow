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

from awf._atomic import atomic_write_text
from awf.xdg import xdg_config_home  # AUD-12: consolidated

log = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
SKILLS_DIR_NAME = "agent-workflow-ui"

# RUN6 #5 (TODO-0060): the supervisor doctrine as a global skill.
# (bundled filename, installed skill directory)
SUPERVISOR_SKILL_FILENAME = "SKILL.awf-supervisor.md"
SUPERVISOR_SKILLS_DIR_NAME = "awf-supervisor"


def _bundled_skill_path(filename: str = SKILL_FILENAME) -> Path:
    """Path to a skill file bundled inside the installed package."""
    return Path(__file__).resolve().parent / filename


def _target_skill_path(skills_dir: str = SKILLS_DIR_NAME) -> Path:
    """Path where opencode expects the skill: ~/.config/opencode/skills/<name>/SKILL.md."""
    return xdg_config_home() / "opencode" / "skills" / skills_dir / SKILL_FILENAME


def _install_skill(
    bundled_filename: str = SKILL_FILENAME, skills_dir: str = SKILLS_DIR_NAME
) -> bool:
    """Copy one bundled skill file to the opencode skills directory.

    Shared by both skills (the plugin's own agent-workflow-ui skill and the
    awf-supervisor doctrine skill, RUN6 #5 / TODO-0060).

    Returns:
        True if skill is in place (copied or already current), False on failure.
    """
    bundled = _bundled_skill_path(bundled_filename)
    target = _target_skill_path(skills_dir)

    if not bundled.exists():
        log.warning("Bundled %s not found at %s — skipping install", bundled.name, bundled)
        return False

    try:
        bundled_content = bundled.read_text(encoding="utf-8")
    except OSError as e:
        log.error("Failed to read bundled %s: %s", bundled.name, e)
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
        # AUD09-08: atomic write (tmp + rename) — a crash mid-write used to
        # leave a truncated SKILL.md until the next start. Also 0600, like
        # the rest of the plugin's persistent artifacts (T2.6).
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, bundled_content, encoding="utf-8")
        log.info("Installed %s → %s", bundled.name, target)
        return True
    except OSError as e:
        log.error("Failed to install %s to %s: %s", bundled.name, target, e)
        return False


def ensure_skill_installed() -> bool:
    """Ensure SKILL.md is installed in opencode skills directory.

    Returns:
        True if skill is in place (copied or already current), False on failure.
    """
    return _install_skill()


def ensure_supervisor_skill_installed() -> bool:
    """Ensure the awf-supervisor doctrine skill is installed (RUN6 #5).

    Same self-healing contract as :func:`ensure_skill_installed`: the skill
    is a SKILL.md in ``~/.config/opencode/skills/awf-supervisor/`` so
    opencode can load it on trigger («работай супервизором awf»).
    """
    return _install_skill(SUPERVISOR_SKILL_FILENAME, SUPERVISOR_SKILLS_DIR_NAME)
