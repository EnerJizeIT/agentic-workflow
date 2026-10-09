"""U2 gate helper: every awf subprocess.run/Popen must carry a timeout.

Walks ``awf/`` with ``ast`` (multi-line calls included), finds calls to
``subprocess.run`` / ``subprocess.Popen`` — plain, aliased
(``import subprocess as _s``) or from-imported
(``from subprocess import Popen as P``) — and checks each call for a
``timeout=`` keyword. The callee names are resolved per file from that
file's own import statements, so a renamed import cannot hide a call
site. Calls without a timeout are compared against the explicit
allowlist below — an exact match is green, anything else (new offender,
or an allowed one silently fixed) is red, so every change to this
surface is a conscious decision.

Denominator is printed on every run: a gate that found no call sites
exits 2 (measured nothing), not 0.

Allowlist (honest exceptions, each with its reason):
- awf/_proc.py (1): ``run_tree`` — the timeout is enforced by
  ``communicate(timeout=...)`` right after the Popen; that is the wrapper
  contract (AUD04-06).
- awf/api/_background.py (1): detached background pipeline — a long-lived
  process by design, stopped via ``awf_kill``, not a timeout.
- awf/signal_watch.py (1): worker Popen — a hard timeout is enforced by
  the deadline loop (``time.monotonic() + hard_timeout``) that kills the
  whole process group.

History: awf/api/pipeline.py (2) carried the U2 FINDING (2026-09-20) —
the rollback path's ``git diff`` / ``git reset`` ran without timeout.
Closed in FU-19 (TODO-0023): both calls now carry ``timeout=30`` and
degrade to AwfApiError.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

EXPECTED_NO_TIMEOUT: dict[str, int] = {
    "awf/_proc.py": 1,
    "awf/api/_background.py": 1,
    "awf/signal_watch.py": 1,
}

MIN_CALL_SITES = 20  # awf/ has 25+ today; below this the gate looks at nothing

GATE_MEMBERS = ("run", "Popen")


def resolve_subprocess_names(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """Names this file binds to subprocess or its gated members.

    Returns ``(module_aliases, member_aliases)``:
    - ``module_aliases``: the name per ``import subprocess`` and
      ``import subprocess as X`` — a call ``<name>.run(...)`` or
      ``<name>.Popen(...)`` is in scope;
    - ``member_aliases``: the name per ``from subprocess import run`` /
      ``from subprocess import Popen as Y`` — a direct call ``<name>(...)``
      is in scope.
    """
    module_aliases: set[str] = set()
    member_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in GATE_MEMBERS:
                    member_aliases[alias.asname or alias.name] = alias.name
    return module_aliases, member_aliases


def _has_timeout(call: ast.Call) -> bool:
    return any(
        kw.arg is not None and kw.arg.startswith("timeout") for kw in call.keywords
    )


def analyze_file(path: Path) -> tuple[int, int]:
    """Count the gated call sites in one file.

    Returns ``(total, without_timeout)``. A call is gated when its callee
    is a name resolved by :func:`resolve_subprocess_names`: an attribute
    call ``<module_alias>.run/Popen`` or a direct call to a from-import
    alias.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_aliases, member_aliases = resolve_subprocess_names(tree)
    total = 0
    without_timeout = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr not in GATE_MEMBERS or not isinstance(func.value, ast.Name):
                continue
            if func.value.id not in module_aliases:
                continue
        elif isinstance(func, ast.Name):
            if func.id not in member_aliases:
                continue
        else:
            continue
        total += 1
        if not _has_timeout(node):
            without_timeout += 1
    return total, without_timeout


def main() -> int:
    root = Path("awf")
    if not root.is_dir():
        print("check-subprocess-timeouts: no awf/ directory", file=sys.stderr)
        return 2

    total = 0
    no_timeout: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            sites, missing = analyze_file(path)
        except SyntaxError as e:
            print(f"check-subprocess-timeouts: cannot parse {path}: {e}", file=sys.stderr)
            return 2
        total += sites
        if missing:
            rel = str(path)
            no_timeout[rel] = no_timeout.get(rel, 0) + missing

    if total < MIN_CALL_SITES:
        print(
            f"check-subprocess-timeouts: only {total} call sites found "
            f"(expected >= {MIN_CALL_SITES}) — the gate is measuring nothing",
            file=sys.stderr,
        )
        return 2

    print(f"timeout-gate: {total} subprocess.run/Popen sites in awf/; without timeout:")
    for name in sorted(set(no_timeout) | set(EXPECTED_NO_TIMEOUT)):
        print(f"  {name}: found={no_timeout.get(name, 0)} expected={EXPECTED_NO_TIMEOUT.get(name, 0)}")

    if no_timeout != EXPECTED_NO_TIMEOUT:
        print(
            "check-subprocess-timeouts: FAIL — the no-timeout set does not match "
            "the allowlist. New call without timeout: add it to EXPECTED_NO_TIMEOUT "
            "only with a written reason. Allowed call fixed: drop the entry.",
            file=sys.stderr,
        )
        return 1
    print("check-subprocess-timeouts: ok (allowlist matches reality)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
