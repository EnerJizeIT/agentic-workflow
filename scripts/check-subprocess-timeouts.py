"""U2 gate helper: every awf subprocess.run/Popen must carry a timeout.

Walks ``awf/`` with ``ast`` (multi-line calls included), finds
``subprocess.run(...)`` / ``subprocess.Popen(...)`` / ``_sp.run(...)``,
and checks each call for a ``timeout=`` keyword. Calls without one are
compared against the explicit allowlist below — an exact match is green,
anything else (new offender, or an allowed one silently fixed) is red, so
every change to this surface is a conscious decision.

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
- awf/api/pipeline.py (2): U2 FINDING (2026-09-20): the rollback path's
  ``git diff`` (dry-run) and ``git reset`` run without timeout. Ratcheted
  here on purpose — fix it in a follow-up and drop the entry; the gate
  will fail until the allowlist is updated to match reality.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

EXPECTED_NO_TIMEOUT: dict[str, int] = {
    "awf/_proc.py": 1,
    "awf/api/_background.py": 1,
    "awf/signal_watch.py": 1,
    "awf/api/pipeline.py": 2,
}

MIN_CALL_SITES = 20  # awf/ has 25+ today; below this the gate looks at nothing


def main() -> int:
    root = Path("awf")
    if not root.is_dir():
        print("check-subprocess-timeouts: no awf/ directory", file=sys.stderr)
        return 2

    total = 0
    no_timeout: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"check-subprocess-timeouts: cannot parse {path}: {e}", file=sys.stderr)
            return 2
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("run", "Popen"):
                continue
            callee = node.func.value
            if not isinstance(callee, ast.Name) or callee.id not in ("subprocess", "_sp"):
                continue
            total += 1
            has_timeout = any(
                kw.arg is not None and kw.arg.startswith("timeout") for kw in node.keywords
            )
            if not has_timeout:
                rel = str(path)
                no_timeout[rel] = no_timeout.get(rel, 0) + 1

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
