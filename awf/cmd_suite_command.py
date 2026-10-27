"""Entry point for ``awf suite-command``.

CARRY IMP2 (R13/K14): print the canonical full-suite command so the shell
helper does not carry a second copy of the pytest literal. The exit code is
the contract the script branches on:

- 0 — command from ``verification.test_cmd`` (config);
- 1 — legacy pytest fallback (the key is absent);
- 2 — suite not configured (``null``, empty string, broken config).
"""
from __future__ import annotations

import sys
from typing import Any

from . import suite_command


def run(args: Any) -> int:
    """Print the canonical command; the exit code is the contract above."""
    resolved = suite_command.resolve(getattr(args, "project_dir", "."))
    if resolved is None:
        print(
            "suite not configured: set verification.test_cmd or AWF_SUITE_CMD",
            file=sys.stderr,
        )
        return 2
    print(resolved.command)
    return 0 if resolved.source == suite_command.CONFIG_SOURCE else 1
