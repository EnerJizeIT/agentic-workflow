"""BD-22/25: subprocess environment setup for opencode run.

Centralized so signal_watch + supervisor + agent_stage can use it without
circular imports through orchestrator.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# AUD14-07: load libc in the PARENT at import time. CDLL inside preexec_fn
# does dlopen after fork in a multi-threaded process (orchestrator runs a
# dashboard thread) — unsafe. preexec itself must only call prctl.
_libc = None
if sys.platform == "linux":
    try:
        import ctypes

        _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        _libc = None


def _pdeathsig_preexec() -> None:
    """DF6-8: Set PR_SET_PDEATHSIG so child dies when parent (orchestrator) dies.

    Linux-only. On other platforms, no-op (best effort).
    Prevents orphan worker subprocesses from continuing after orchestrator crash.
    """
    if _libc is None:
        return
    try:
        import signal as _signal

        PR_SET_PDEATHSIG = 1
        _libc.prctl(PR_SET_PDEATHSIG, _signal.SIGTERM)
    except Exception:
        pass  # best effort — don't crash if libc/prctl unavailable


def _is_readonly_role(role: str, project_dir: str | Path | None) -> bool:
    """W7: True when ``role`` is in the project's ``automation.readonly_roles``.

    The list lives in ``.agentic/config.yaml`` and is EMPTY by default —
    behavior without it is exactly the previous one. Any read error
    (no .agentic/, malformed YAML, wrong value type) degrades to False:
    env assembly for a spawn must never raise.
    """
    if not role or project_dir is None:
        return False
    try:
        from . import config as cfg_mod

        config = cfg_mod.load(project_dir)
        roles = cfg_mod.get(config, "automation.readonly_roles", [])
    except Exception:
        return False
    if not isinstance(roles, list):
        return False
    return role in {str(r) for r in roles}


def awf_subprocess_env(
    *,
    role: str = "",
    project_dir: str | Path | None = None,
) -> dict[str, str]:
    """BD-22/KAUD-5: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to override permission rules so
    the subprocess can run ``edit``/``bash``/``write`` without prompting.

    W7 (readonly roles): when ``role`` is listed in the project's
    ``automation.readonly_roles`` (``.agentic/config.yaml``, default
    empty), the ``edit``/``write`` overrides are NOT emitted — the role
    gets read/bash/webfetch without the file-write tools. ``bash``/
    ``webfetch`` and everything else stay as before.

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
    # W7: a readonly role (automation.readonly_roles) does not get the
    # edit/write overrides — the user's own opencode.json values (if any)
    # still apply, awf just stops granting the write tools.
    readonly = _is_readonly_role(role, project_dir)
    overrides = {
        "bash": "allow",
        "webfetch": "allow",
    }
    if not readonly:
        overrides["edit"] = "allow"
        overrides["write"] = "allow"
    merged_permissions.update(overrides)
    merged["permission"] = merged_permissions

    # P1 security: only serialize permission overrides to env — never API keys,
    # providers, or other sensitive fields from opencode.json. Workers only
    # need the permission overrides; everything else is loaded by opencode
    # itself from the real config file.
    config_json = json.dumps({"permission": merged_permissions})
    env["OPENCODE_CONFIG_CONTENT"] = config_json

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
