"""BD-22/25: subprocess environment setup for opencode run.

Centralized so signal_watch + supervisor + agent_stage can use it without
circular imports through orchestrator.
"""
from __future__ import annotations

import json
import os


def awf_subprocess_env() -> dict[str, str]:
    """BD-22: env for opencode subprocess spawned by awf.

    Sets ``OPENCODE_CONFIG_CONTENT`` to override global permission rules so
    the subprocess can run ``edit``/``bash``/``write`` without prompting
    the user.

    BD-25: strip ``OPENCODE_SERVER_*`` env vars so the subprocess does NOT
    attach to a running ``opencode serve`` instance (was sharing state).
    """
    env = os.environ.copy()
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
        "permission": {
            "edit": "allow",
            "bash": "allow",
            "write": "allow",
            "webfetch": "allow",
        }
    })
    for key in list(env.keys()):
        if key.startswith("OPENCODE_SERVER") or key.startswith("OPENCODE_HOST"):
            env.pop(key, None)
    return env
