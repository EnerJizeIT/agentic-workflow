#!/usr/bin/env python3
"""Process-safety gate: no real signal may target a pid/pgid at or below 1.

`docs/contracts/process-safety.md` used to grep source text for
``kill(-1)``-style tokens. That caught prose too: a docstring explaining the
2026-09-20 incident turned the gate red and invited rewriting a warning
comment. A security gate that fires on comments is worse than no gate — it
teaches people to silence it.

This scanner reads the AST: only a CALL to ``kill``/``killpg`` (bare or as a
method) whose TARGET argument is the integer constant 0, 1 or -1 is a
violation. Mentions in comments, docstrings and strings are invisible to it;
a real call with a literal target is reported as ``path:line: <text>``.
Dynamic targets (a variable) are left to review — the gate never guesses.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SIGNAL_NAMES = {"kill", "killpg"}
# The keyword under which a signal call names its target (os.kill — ``pid``,
# os.killpg — ``pgid``).
TARGET_KEYWORDS = {"pid", "pgid"}


def _os_aliases(tree: ast.AST) -> set[str]:
    """Every name the module imported ``os`` under (``os``, ``_os``, ...)."""
    aliases = {"os"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    aliases.add(alias.asname or "os")
    return aliases


def _is_signal_call(node: ast.Call, os_aliases: set[str]) -> bool:
    """Is this call ``os.kill``/``os.killpg`` or the bare imported name?

    A method that merely shares the name (``task.kill(1)``) is not the
    kernel call; flagging it teaches people to silence the gate.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in SIGNAL_NAMES
    if isinstance(func, ast.Attribute):
        return (
            func.attr in SIGNAL_NAMES
            and isinstance(func.value, ast.Name)
            and func.value.id in os_aliases
        )
    return False


def _target_arg(node: ast.Call) -> ast.AST | None:
    """The pid/pgid argument — positional first, then a keyword form."""
    if node.args:
        return node.args[0]
    for keyword in node.keywords:
        if keyword.arg in TARGET_KEYWORDS:
            return keyword.value
    return None


def _literal_target(node: ast.AST) -> int | None:
    """The integer value of a literal target expression, else None."""
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        return -node.operand.value
    return None


def violations(source: str) -> list[tuple[int, ast.Call]]:
    """(line, call) for every literal signal target at or below 1."""
    tree = ast.parse(source)
    os_aliases = _os_aliases(tree)
    found: list[tuple[int, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not _is_signal_call(node, os_aliases):
            continue
        arg = _target_arg(node)
        target = _literal_target(arg) if arg is not None else None
        if target is not None and target <= 1:
            found.append((node.lineno, node))
    return sorted(found, key=lambda item: item[0])


def scan_paths(paths: list[str]) -> int:
    """Print every violation; 1 when any exists, 0 when the tree is clean."""
    bad = 0
    for raw in paths:
        root = Path(raw)
        if not root.exists():
            continue
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            if "__pycache__" in path.parts:
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            try:
                found = violations(source)
            except SyntaxError:
                continue
            lines = source.splitlines()
            for lineno, _node in found:
                text = lines[lineno - 1].strip() if lineno <= len(lines) else ""
                print(f"{path}:{lineno}: {text}")
                bad += 1
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: check_process_safety.py <path> [<path> ...]", file=sys.stderr)
        return 2
    return scan_paths(args)


if __name__ == "__main__":
    raise SystemExit(main())
