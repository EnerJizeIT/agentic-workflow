"""U11 (B6): mutation smoke as a supervisor tool — ``awf mutations``.

Runs the mutations of ``scripts/mutations.txt`` on a QUIET tree and
reports killed/survived. The format is read-only (one mutation per line,
``FILE @@ FIND @@ REPL @@ CMD``); ``scripts/mutation-smoke.sh`` stays the
CI shell implementation, this is the interactive equivalent with the same
semantics: refuse unless the tree is quiet, restore every file (also when
the runner raises), exit 0 = all killed, exit 1 = something survived or
the run was refused, exit 2 = configuration error (bad line, stale
mutation, empty list).

The test command is a killable stage: it runs in its own session
(``run_tree``), on timeout the whole process group dies (no orphaned
pytest between mutations), and it carries ``PYTHONDONTWRITEBYTECODE=1``
+ mtime bump so bytecode caches cannot mistake the mutated file for the
original.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import git_utils
from ._proc import run_tree

#: Literal field separator, same as mutation-smoke.sh.
SEPARATOR = " @@"

#: ``runner(cmd, cwd, timeout) -> (returncode, output)``. 124 = timeout
#: (the default runner mirrors the shell convention).
Runner = Callable[[str, Path, int], tuple[int, str]]

DEFAULT_TIMEOUT = 1800


class MutationConfigError(Exception):
    """Configuration error (exit 2): bad line, empty list, stale
    mutation, missing target file."""


class MutationRefused(Exception):
    """Refusal (exit 1): not a git repo or a dirty tree — the run cannot
    be trusted, and the restore cannot be guaranteed."""


@dataclass(frozen=True)
class Mutation:
    file: str
    find: str
    repl: str
    cmd: str
    lineno: int


@dataclass
class MutationOutcome:
    index: int
    mutation: Mutation
    status: str  # "killed" | "survived" | "timeout"
    tail: str = ""


@dataclass
class MutationReport:
    outcomes: list[MutationOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def killed(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "killed")

    @property
    def survived(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "survived")

    @property
    def timeouts(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "timeout")

    @property
    def ok(self) -> bool:
        return self.survived == 0 and self.timeouts == 0


def parse_mutation_lines(text: str) -> list[Mutation]:
    """Parse mutation lines; skip blanks and ``#`` comments.

    A line needs four fields separated by `` @@ `` (file, find, repl,
    cmd). ``repl`` may be empty (deletion mutation); ``cmd`` may itself
    contain the separator (everything after the third separator).
    """
    mutations: list[Mutation] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(SEPARATOR)
        if len(parts) < 4:
            raise MutationConfigError(
                f"line {lineno}: need 4 fields separated by '{SEPARATOR}' "
                f"(file @@ find @@ repl @@ cmd), got {len(parts)}"
            )
        file_, find, repl = (p.strip() for p in parts[:3])
        cmd = SEPARATOR.join(parts[3:]).strip()
        if not file_ or not find or not cmd:
            raise MutationConfigError(
                f"line {lineno}: file, find and cmd must be non-empty"
            )
        mutations.append(Mutation(file_, find, repl, cmd, lineno))
    return mutations


def load_mutations(path: Path) -> list[Mutation]:
    """Read + parse a mutations file; an empty list is a config error
    (the shell's 'nothing to check is not a green result')."""
    path = Path(path)
    if not path.is_file():
        raise MutationConfigError(f"no such file: {path}")
    mutations = parse_mutation_lines(path.read_text(encoding="utf-8"))
    if not mutations:
        raise MutationConfigError(
            f"no mutations in {path} — nothing to check is not a green result"
        )
    return mutations


def _default_runner(cmd: str, cwd: Path, timeout: int) -> tuple[int, str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # run_tree (not subprocess.run): the test command is a killable stage —
    # it starts in its own session, and on timeout the WHOLE process group
    # dies. A direct-child-only kill would orphan the real pytest while the
    # next mutation already edits files (process-safety contract, _proc.py
    # rule: every awf subprocess with a kill path kills the group).
    try:
        result = run_tree(
            cmd,
            shell=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _tail(text: str, lines: int = 10) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])


def run_mutations(
    project_dir: Path | str,
    mutations_file: Path | str,
    *,
    runner: Runner | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> MutationReport:
    """Run all mutations of ``mutations_file`` on a QUIET tree.

    Refuses (MutationRefused) when the directory is not a git repo or the
    tree is dirty. Aborts (MutationConfigError) on a bad/stale mutation —
    before touching anything. Every mutated file is restored in a
    ``finally`` (content + mtime), even when the runner raises.
    """
    pd = Path(project_dir).resolve()
    if not git_utils.is_git_repo(pd):
        raise MutationRefused(
            f"{pd} is not a git repo — a quiet tree cannot be guaranteed; "
            "run mutations inside a repo"
        )
    status = git_utils.status_porcelain(pd)
    if status.strip():
        raise MutationRefused(
            "the working tree is dirty (git status not empty) — commit or "
            "revert the changes before a mutation run, otherwise the "
            "mutations may conflict with them:\n" + status[:500]
        )

    mutations = load_mutations(Path(mutations_file))
    run = runner or _default_runner
    report = MutationReport()
    for i, m in enumerate(mutations, 1):
        target = pd / m.file
        if not target.is_file():
            raise MutationConfigError(
                f"line {m.lineno}: no such file {m.file} — the mutation is stale"
            )
        try:
            original = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise MutationConfigError(
                f"line {m.lineno}: {m.file} is not UTF-8 text — a literal "
                "replacement is not possible"
            ) from e
        if m.find not in original:
            raise MutationConfigError(
                f"line {m.lineno}: '{m.find[:60]}' not found in {m.file} — "
                "the mutation is stale (the code changed)"
            )
        st = target.stat()
        target.write_text(original.replace(m.find, m.repl, 1), encoding="utf-8")
        # mtime bump: caches that compare mtime+size (bytecode) must not
        # accept the mutated file as the original. cp -p in the shell did
        # the same on restore; here we restore the exact original times.
        os.utime(target, (time.time(), time.time() + 5))
        try:
            rc, out = run(m.cmd, pd, timeout)
        finally:
            target.write_text(original, encoding="utf-8")
            # ns-exact: float seconds would truncate and fail the restore
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        if rc == 0:
            status_name = "survived"
        elif rc == 124:
            status_name = "timeout"
        else:
            status_name = "killed"
        report.outcomes.append(
            MutationOutcome(i, m, status_name, _tail(out))
        )
    return report
