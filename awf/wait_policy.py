"""Leaf wait policy: the single-wait cap (RUN6 #3).

Lives outside ``awf.api`` so light callers (the brief card) read the cap
without importing the heavy API package. ``awf.api.wait_event`` re-exports
these names, so ``awf.api.wait_cap`` / ``TRANSPORT_CAP`` keep working.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# B3 (run2 report): the default MCP client transport (~60s timeout) cuts a
# single wait at ~55s — the measured working cap. This is the DEFAULT; when
# the owner's transport tolerates a longer single wait, the cap is raised
# via .agentic/config.yaml `wait.cap_seconds` or env AWF_WAIT_CAP (env wins,
# RUN6 #3) — see wait_cap(). suggested_timeout must stay at or under it, or
# the supervisor's next call dies mid-wait (-32001).
TRANSPORT_CAP = 55


def _parse_cap(value: Any) -> int | None:
    """A positive int usable as a wait cap; None when not a valid one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
    elif isinstance(value, str):
        try:
            n = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return n if n > 0 else None


def wait_cap(project_dir: Path | None = None) -> int:
    """RUN6 #3: the honest single-wait cap for this project.

    Priority: env ``AWF_WAIT_CAP`` > ``wait.cap_seconds`` in
    ``.agentic/config.yaml`` > ``TRANSPORT_CAP`` (default 55s). An invalid
    value falls through to the next source with a stderr warning, so the
    default always works.
    """
    raw = os.environ.get("AWF_WAIT_CAP")
    if raw is not None:
        cap = _parse_cap(raw)
        if cap is not None:
            return cap
        print(
            f"WARNING: AWF_WAIT_CAP={raw!r} is not a positive integer — "
            "falling back to the config/default cap",
            file=sys.stderr,
        )
    if project_dir is not None:
        from . import config as _cfg

        value = _cfg.get(_cfg.load(project_dir), "wait.cap_seconds")
        if value is not None:
            cap = _parse_cap(value)
            if cap is not None:
                return cap
            print(
                f"WARNING: wait.cap_seconds={value!r} is not a positive "
                "integer — using the default cap",
                file=sys.stderr,
            )
    return TRANSPORT_CAP
