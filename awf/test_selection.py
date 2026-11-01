"""A21: which tests a change actually needs.

The suite is 6350 tests and ~10 minutes; a unit touches one to three files.
Running everything on every attempt is the largest cost of a run, so this
module answers one question: given the changed files, which test files are
worth running?

The rules are deliberately conservative — a selector that misses a
regression is worse than a slow suite:

* **core** files (the engine's own plumbing) and test scaffolding force the
  FULL suite: they change the behaviour every other test depends on;
* everything else contributes test files by three independent signals —
  import (``from awf.x import`` / ``import awf.x``), file name
  (``test_x.py``), and a literal mention of the module path or name (config
  keys, CLI commands, probes) — plus that test directory's neighbours;
* a selection larger than ``FULL_RATIO`` of the suite is pointless to
  filter: run everything;
* guard/inventory tests are added by rule, never by import.

The answer is a list of files, not node ids: a file-level selection can grow
without anyone noticing it silently dropped a case.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Changed files that force the full suite: the engine's own plumbing, the
#: test harness and the packaging. A unit that touches these is not a small
#: unit, whatever the diff size says.
CORE_PATTERNS: tuple[str, ...] = (
    "awf/pipeline_engine.py",
    "awf/agent_stage.py",
    "awf/orchestrator.py",
    "awf/supervisor.py",
    "awf/signals.py",
    "awf/verify_pack.py",
    "awf/pipeline.py",
    "awf/api/pipeline.py",
    "awf/api/_background.py",
    "awf/api/_lease.py",
    "awf/pipeline_state.py",
    "awf/run_state.py",
    "awf/infra/",
    "tests/conftest.py",
    "pyproject.toml",
)

#: Cheap guards that run on every selection: version drift and the 2.x
#: boundary are one-line failures nobody should discover late.
GUARD_TESTS: tuple[str, ...] = (
    "tests/unit/test_version_sync.py",
    "tests/unit/test_opencode2_only_guard.py",
)

#: The inventory/design tests scan every module — they are the anti-lie
#: guard, and they are slow (3 minutes together). They join when the change
#: is in the package or its design docs, which is exactly when they can fail.
INVENTORY_TESTS: tuple[str, ...] = (
    "tests/negative/test_design_agentic_writers.py",
    "tests/negative/test_docs_counters.py",
)

#: Above this share of the suite, filtering buys nothing.
FULL_RATIO = 0.4

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE
)


@dataclass
class Selection:
    files: list[str]
    full: bool
    reason: str
    changed: list[str] = field(default_factory=list)
    total_tests: int = 0
    selected_tests: int = 0

    def as_text(self) -> str:
        return "\n".join(self.files) + ("\n" if self.files else "")


def _git(project: Path, *args: str) -> str:
    """One owner for git access: ``git_utils`` (the M4 guard's rule)."""
    from .git_utils import git_stdout

    try:
        # surrogateescape: `-z` output is raw bytes; a non-UTF-8 name must not
        # raise here (the M4 invariant: read paths with the NUL protocol).
        return git_stdout(project, *args, check=False, errors="surrogateescape")
    except (OSError, subprocess.SubprocessError):
        return ""


def changed_files(project: Path, base: str = "") -> list[str]:
    """Files changed vs ``base`` PLUS the uncommitted worktree.

    A unit's work is uncommitted by design (the commit gate runs at verify),
    so ``base..HEAD`` alone would report an empty diff on a real unit.
    """
    names: set[str] = set()
    if base:
        # -z: the NUL protocol — a non-UTF-8 path never breaks the listing
        # (M4's rule for every printed git path read).
        out = _git(project, "diff", "--name-only", "--diff-filter=ACMR", "-z", base)
        names.update(part for part in out.split("\0") if part)
    out = _git(project, "status", "--porcelain", "-z")
    for entry in out.split("\0"):
        entry = entry.rstrip("\n")
        if not entry:
            continue
        path = entry[3:].strip()
        if " -> " in path:  # rename: take the destination
            path = path.split(" -> ", 1)[1]
        if path:
            names.add(path)
    return sorted(names)


def _module_names(path: str) -> set[str]:
    """``awf/api/pipeline.py`` → ``awf.api.pipeline`` + ``awf/api/pipeline``."""
    if not path.endswith(".py"):
        return {path}
    dotted = path[: -len(".py")].replace("/", ".")
    if dotted.endswith(".__init__"):
        dotted = dotted[: -len(".__init__")]
    return {dotted, path[: -len(".py")]}


def build_import_index(project: Path) -> dict[str, set[str]]:
    """``module name`` → the test files that import it (one parse per run)."""
    tests_dir = project / "tests"
    index: dict[str, set[str]] = {}
    for test_file in tests_dir.rglob("test_*.py"):
        try:
            text = test_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(test_file.relative_to(project))
        for left, right in _IMPORT_RE.findall(text):
            module = left or right
            if not module:
                continue
            index.setdefault(module, set()).add(rel)
            # `from awf.api import pipeline` imports awf.api.pipeline too.
            index.setdefault(module + ".__init__", set()).add(rel)
    return index


def _test_files(project: Path) -> list[str]:
    return sorted(
        str(p.relative_to(project))
        for p in (project / "tests").rglob("test_*.py")
    )


def _mentions(project: Path, needles: set[str], candidates: list[str]) -> set[str]:
    """Test files whose TEXT mentions one of the needles (config/CLI probes)."""
    hits: set[str] = set()
    for rel in candidates:
        try:
            text = (project / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            hits.add(rel)
    return hits


def _is_core(path: str) -> bool:
    return any(path == core or path.startswith(core) for core in CORE_PATTERNS)


def select(
    project: str | Path,
    *,
    base: str = "",
    force_full: bool = False,
) -> Selection:
    """The test files a change needs, or the full suite with the reason."""
    project = Path(project).resolve()
    all_tests = _test_files(project)
    changed = changed_files(project, base)

    if force_full:
        return Selection(all_tests, True, "forced by the caller", changed, len(all_tests), len(all_tests))
    if not changed:
        return Selection(
            all_tests, True, "no diff to select on — full suite", changed, len(all_tests), len(all_tests)
        )

    core = [path for path in changed if _is_core(path)]
    if core:
        return Selection(
            all_tests,
            True,
            "core change: " + ", ".join(core),
            changed,
            len(all_tests),
            len(all_tests),
        )

    selected: set[str] = {path for path in changed if path.startswith("tests/")}
    index = build_import_index(project)
    candidates = all_tests

    for path in changed:
        if not path.endswith(".py"):
            continue
        for module in _module_names(path):
            selected |= index.get(module, set())
        stem = Path(path).stem
        selected |= {rel for rel in candidates if Path(rel).stem == f"test_{stem}"}
        selected |= _mentions(project, {path, path[: -len(".py")].replace("/", ".")}, candidates)

    # Neighbours: a change inside a package usually has a sibling suite that
    # exercises the same surface (the name-based match alone misses it).
    for path in changed:
        if path.startswith("awf/"):
            parent = Path(path).parent.as_posix()
            selected |= {
                rel
                for rel in candidates
                if Path(rel).parent.as_posix().endswith(parent.replace("awf/", "", 1))
            }

    selected |= {rel for rel in GUARD_TESTS if (project / rel).is_file()}
    if any(path.startswith("awf/") for path in changed) or any(
        path.startswith("docs/design") for path in changed
    ):
        selected |= {rel for rel in INVENTORY_TESTS if (project / rel).is_file()}

    selected &= set(all_tests)
    if len(selected) > FULL_RATIO * len(all_tests):
        return Selection(
            all_tests,
            True,
            f"selection reached {len(selected)}/{len(all_tests)} files",
            changed,
            len(all_tests),
            len(all_tests),
        )
    return Selection(
        sorted(selected), False, "selected by core/import/name/mention/neighbour", changed, len(all_tests), len(selected)
    )


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="awf.test_selection",
        description="List the test files a change needs (A21).",
    )
    parser.add_argument("--project-dir", default=".")
    parser.add_argument("--base", default="", help="baseline sha (default: worktree only)")
    parser.add_argument("--explain", action="store_true", help="print the reason to stderr")
    parser.add_argument("--full", action="store_true", help="force the full suite")
    args = parser.parse_args(argv)

    result = select(args.project_dir, base=args.base, force_full=args.full)
    if args.explain:
        print(
            f"tests: {result.selected_tests}/{result.total_tests} files "
            f"({'FULL' if result.full else 'selected'}) — {result.reason}",
            file=sys.stderr,
        )
        for path in result.changed[:20]:
            print(f"  changed: {path}", file=sys.stderr)
    sys.stdout.write(result.as_text())
    return 0


if __name__ == "__main__":  # pragma: no cover - thin CLI
    raise SystemExit(_main())
