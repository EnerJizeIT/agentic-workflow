"""BD-22/25: subprocess environment setup for opencode run.

Centralized so signal_watch + supervisor + agent_stage can use it without
circular imports through orchestrator.
"""
from __future__ import annotations

import json
import os
import sys


def _pdeathsig_preexec() -> None:
    """DF6-8: Set PR_SET_PDEATHSIG so child dies when parent (orchestrator) dies.

    Linux-only. On other platforms, no-op (best effort).
    Prevents orphan worker subprocesses from continuing after orchestrator crash.
    """
    if sys.platform != "linux":
        return
    try:
        import ctypes
        import signal as _signal

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, _signal.SIGTERM)
    except Exception:
        pass  # best effort — don't crash if libc/prctl unavailable


def awf_subprocess_env() -> dict[str, str]:
    """BD-22/KAUD-5: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to override permission rules so
    the subprocess can run ``edit``/``bash``/``write`` without prompting.

    KAUD-5: MERGES with user's existing opencode.json instead of replacing.
    Reads user's config, adds our permission overrides on top, preserves
    user's other settings (theme, providers, agents, etc.).

    BD-25: strip ``OPENCODE_SERVER_*`` env vars so the subprocess does NOT
    attach to a running ``opencode serve`` instance.
    """
    env = os.environ.copy()

    # KAUD-5: Start from user's existing config, then merge our overrides
    user_config: dict = {}
    try:
        from .xdg import opencode_config_file
        config_path = opencode_config_file()
        if config_path.is_file():
            user_config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(user_config, dict):
                user_config = {}
    except (OSError, json.JSONDecodeError):
        pass  # If user config is unreadable, start from empty

    # Merge: user config + our permission overrides (ours take priority)
    merged = user_config.copy()
    merged_permissions = merged.get("permission", {})
    if not isinstance(merged_permissions, dict):
        merged_permissions = {}
    merged_permissions.update({
        "edit": "allow",
        "bash": "allow",
        "write": "allow",
        "webfetch": "allow",
    })
    merged["permission"] = merged_permissions

    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(merged)

    # BD-25: strip server env vars
    strip_keys = (
        "OPENCODE_SERVER",
        "OPENCODE_SERVER_URL",
        "OPENCODE_SERVER_TOKEN",
        "OPENCODE_HOST",
        "OPENCODE_HOST_TOKEN",
    )
    for key in list(env.keys()):
        if key in strip_keys:
            env.pop(key, None)
    return env
