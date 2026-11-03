"""A5 (TODO-0366): QA pays for the full suite once, not on every iteration.

The stage used to run ``pytest tests/`` after every internal edit, while the
canonical script already keeps one artifact per ``(tree-sha, command)``
pair. Two rules follow from that:

- an artifact that is current for THIS tree is the evidence — reuse it and
  do not spawn a second run (the raw-run gate treats the same file as the
  proof that a full run was canonical);
- an internal iteration uses the diff selection
  (``scripts/affected-tests.sh --run``) or only the touched files; the one
  canonical full run belongs right before the verdict.

:func:`plan_suite_run` is the single owner of that DECISION (which of the
three actions to take), while :func:`awf.suite_guard.artifact_for` stays the
single owner of the ARTIFACT FACT ("a finished canonical result exists for
this tree and command") — the raw-run gate asks the same fact, so the two
cannot drift apart. The canonical script itself is untouched: it still writes
the artifacts this module reads.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import git_utils, paths, suite_command, suite_guard

#: Decision values — one spelling for callers, the prompt and the tests.
REUSE = "reuse"
SELECT = "select"
FULL = "full"

#: The line the stage prints into its log; counted runs, not minutes of prose.
LOG_PREFIX = "qa: full-suite"


@dataclass(frozen=True)
class SuiteDecision:
    """What the QA stage should do next with the full suite."""

    action: str
    reason: str
    artifact: Path | None = None


def artifact_path(project_dir: str | Path, sha: str, command: str) -> Path:
    """``.agentic/context/SUITE-<tree-sha>-<cmd8>.json`` — the writer's name.

    ``cmd8`` is the first 8 hex of the command hash: the same convention the
    canonical script uses, so a narrow run never overwrites the canonical
    artifact. This is a MIRROR of the writer, for tests and for reports —
    ``suite_guard.artifact_for`` does the lookup, so a name drift shows up as
    a red test (``test_script_artifact_matches_cli``) rather than as a silent
    cache miss.
    """
    cmd8 = hashlib.sha256(command.encode()).hexdigest()[:8]
    return paths.context_dir(project_dir) / f"SUITE-{sha}-{cmd8}.json"


def current_artifact(project_dir: str | Path) -> Path | None:
    """The finished canonical artifact for the CURRENT tree and command, or ``None``.

    Only the two inputs are resolved here (the canonical command and the tree
    sha); the artifact fact itself — same tree, same command, an integer
    ``exit`` — belongs to :func:`awf.suite_guard.artifact_for`, which the
    raw-run gate reads too. Anything unreadable or unresolvable is "no
    artifact", not an error: the caller then falls back to a real run.
    """
    canonical = suite_command.resolve(project_dir)
    if canonical is None:
        return None
    try:
        sha = git_utils.tree_fingerprint(project_dir)
    except RuntimeError:
        return None
    return suite_guard.artifact_for(project_dir, sha, canonical.command)


def plan_suite_run(
    project_dir: str | Path, *, internal_iteration: bool = False
) -> SuiteDecision:
    """Decide: reuse the current artifact, select by diff, or run the full suite.

    Reuse wins over everything: a second canonical run on an unchanged tree
    costs the same and proves nothing new (the script would even return the
    cached artifact). Without one, an internal iteration gets the diff
    selection and only the step before the verdict pays for the full run.
    """
    record = current_artifact(project_dir)
    if record is not None:
        return SuiteDecision(
            REUSE,
            "актуальный канонический артефакт на этом дереве — сослаться "
            "на него, полный прогон не повторять",
            record,
        )
    if internal_iteration:
        return SuiteDecision(
            SELECT,
            "внутренняя итерация — селекция по диффу "
            "(scripts/affected-tests.sh --run) или только затронутые файлы",
        )
    return SuiteDecision(
        FULL,
        "перед вердиктом — один канонический прогон "
        "(scripts/suite-canonical.sh)",
    )


@dataclass
class RunJournal:
    """Counted runs and their durations — the stage report and the log line."""

    runs: list[tuple[str, float, str]] = field(default_factory=list)

    def record(self, kind: str, seconds: float, what: str = "") -> None:
        self.runs.append((kind, float(seconds), what))

    def log_line(self) -> str:
        """``qa: full-suite runs=N, selected runs=M, total=Ns``."""
        full = sum(1 for kind, _, _ in self.runs if kind == FULL)
        selected = sum(1 for kind, _, _ in self.runs if kind == SELECT)
        total = sum(seconds for _, seconds, _ in self.runs)
        return f"{LOG_PREFIX} runs={full}, selected runs={selected}, total={total:.0f}s"

    def report_lines(self) -> list[str]:
        """Per-run rows plus the summary: full flag, duration, what ran."""
        rows = [
            f"- {kind}: {seconds:.1f}s — {what or 'без описания'}"
            for kind, seconds, what in self.runs
        ]
        return rows + [self.log_line()]


__all__ = [
    "FULL",
    "LOG_PREFIX",
    "REUSE",
    "SELECT",
    "RunJournal",
    "SuiteDecision",
    "artifact_path",
    "current_artifact",
    "plan_suite_run",
]
