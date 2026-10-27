"""Entry point for ``awf suite-command``.

CARRY IMP2 (R13/K14): print the canonical full-suite command so the shell
helper does not carry a second copy of the pytest literal. The exit code is
the contract the script branches on:

- 0 — command from ``verification.test_cmd`` (config);
- 1 — legacy pytest fallback (the key is absent);
- 2 — suite not configured (``null``, empty string, non-string value);
- 5 — ``.agentic/config.yaml`` is unreadable/broken (its own code: the file
  cannot be trusted, so the legacy pytest fallback is not safe either).

The shell helper branches on 5 separately: «broken file» and «empty key»
must not share a message (K14, CTX1 volut 4).

``--runner COMMAND`` is the second mode (AUD-27-3): print the runner name
from the shared parser and exit 0. It answers no project question, so this
mode reads no config and a broken one cannot fake a runner failure.
"""
from __future__ import annotations

import sys
from typing import Any

from . import command_parse, suite_command


def run(args: Any) -> int:
    """Print the canonical command; the exit code is the contract above.

    ``--runner COMMAND`` is the other question this command answers: the
    runner name of a shell line, from the shared parser (AUD-27-3). The
    shell helper uses it for the artifact's ``runner``/``counts`` fields,
    so the script is not a third parser.
    """
    runner = getattr(args, "runner", None)
    if runner is not None:
        print(command_parse.detect_runner(runner))
        return 0
    resolved, state = suite_command.resolve_with_state(
        getattr(args, "project_dir", ".")
    )
    if resolved is None:
        if state == "broken":
            print(
                "suite not configured: broken .agentic/config.yaml",
                file=sys.stderr,
            )
            return 5
        # CTX4: this entry point never reads ``AWF_SUITE_CMD`` (only the
        # shell helper does), so naming it here would advise an override
        # this command silently ignores.
        print(
            "suite not configured: set verification.test_cmd",
            file=sys.stderr,
        )
        return 2
    print(resolved.command)
    return 0 if resolved.source == suite_command.CONFIG_SOURCE else 1
