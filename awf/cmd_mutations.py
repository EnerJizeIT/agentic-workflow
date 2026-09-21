"""Entry point for ``awf mutations`` (U11, B6).

Exit codes follow ``scripts/mutation-smoke.sh``: 0 = all killed, 1 =
something survived or the run was refused (dirty tree / not a repo),
2 = configuration error (bad line, stale mutation, empty list).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import mutations as mut


def run(args: Any) -> int:
    """List or run the mutations of the project's mutations file."""
    project_dir = Path(getattr(args, "project_dir", "."))
    file_path = Path(getattr(args, "file", "scripts/mutations.txt"))
    if not file_path.is_absolute():
        # The default (scripts/mutations.txt) lives in the PROJECT, not in
        # the caller's cwd.
        file_path = project_dir / file_path

    if getattr(args, "list", False):
        try:
            listed = mut.load_mutations(file_path)
        except mut.MutationConfigError as e:
            print(f"mutations: {e}", file=sys.stderr)
            return 2
        for i, m in enumerate(listed, 1):
            print(f"{i}. {m.file}")
            print(f"   «{m.find}» → «{m.repl}»")
            print(f"   {m.cmd}")
        print(f"Total: {len(listed)} mutations")
        return 0

    try:
        report = mut.run_mutations(
            project_dir,
            file_path,
            timeout=getattr(args, "timeout", mut.DEFAULT_TIMEOUT),
        )
    except mut.MutationConfigError as e:
        print(f"mutations: {e}", file=sys.stderr)
        return 2
    except mut.MutationRefused as e:
        print(f"mutations: {e}", file=sys.stderr)
        return 1

    for o in report.outcomes:
        mark = {
            "killed": "✓ killed",
            "survived": "✗ survived",
            "timeout": "✗ timeout",
        }[o.status]
        print(f"{mark}: {o.mutation.file} — «{o.mutation.find}» → «{o.mutation.repl}»")
        print(f"   cmd: {o.mutation.cmd}")
        for line in o.tail.splitlines():
            print(f"   | {line}")
    print(
        f"mutations: total {report.total}; killed {report.killed}; "
        f"survived {report.survived}; timeout {report.timeouts}"
    )
    if report.ok:
        print("All mutations killed — the old tests still protect the code.")
        return 0
    print(
        "A survived mutation is a backlog finding: the named tests do not "
        "protect that code. A timeout means the check did not run to the "
        "end.",
        file=sys.stderr,
    )
    return 1
