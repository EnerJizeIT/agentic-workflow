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
    # BD-25: explicit whitelist of env vars to strip (was wide prefix match
    # which could nuke legit vars like OPENCODE_SERVER_STATUS).
    # These are the ones that cause opencode run to attach to a running
    # 'opencode serve' instance.
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
