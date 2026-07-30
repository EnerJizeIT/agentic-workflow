"""XDG-aware config directory helpers.

Centralizes ~/.config resolution so XDG_CONFIG_HOME is respected
everywhere (was hardcoded Path.home() / ".config" in 9 places).
"""
from __future__ import annotations

import os
from pathlib import Path


def xdg_config_home() -> Path:
    """Return XDG_CONFIG_HOME or ~/.config (A9 fix).

    Per XDG Base Directory Specification:
    https://specifications.freedesktop.org/basedir-spec/latest
    $XDG_CONFIG_HOME defines the base directory relative to which
    user-specific configuration files should be stored. If unset or
    empty, a default equal to $HOME/.config should be used.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".config"


def opencode_config_dir() -> Path:
    """opencode config dir: $XDG_CONFIG_HOME/opencode."""
    return xdg_config_home() / "opencode"


def opencode_config_file() -> Path:
    """opencode.json path."""
    return opencode_config_dir() / "opencode.json"


def opencode_skills_dir() -> Path:
    """opencode skills dir: $XDG_CONFIG_HOME/opencode/skills."""
    return opencode_config_dir() / "skills"


def awf_config_dir() -> Path:
    """awf config dir: $XDG_CONFIG_HOME/awf."""
    return xdg_config_home() / "awf"


def awf_roles_dir() -> Path:
    """awf global roles dir: $XDG_CONFIG_HOME/awf/roles."""
    return awf_config_dir() / "roles"
