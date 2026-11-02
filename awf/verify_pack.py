"""U5: ``awf verify-pack`` — one deterministic report for the supervisor.

The supervisor's manual verify ritual (~10 steps: target tests, safety
canaries, diff minimality, lint, gates, prove-red) costs tokens and is not
reproducible. This module runs the mechanical part once and leaves a report
ready for the supervisor's wake-up: ``.agentic/context/GATES-<todo>.md``
(markdown + a short JSON block at the end for machines).

Sections (each: status + details + denominator):

1. ``gates`` — the fast gates: ``scripts/check-contracts.sh``,
   ``scripts/ratchet.sh``, ``scripts/instruction-gates.sh``. The full pytest
   suite is NOT part of the pack (it belongs to QA) — the report says so.
2. ``safety`` — canaries of the 2026-09-20 incident: the ``pid > 1`` guard
   in ``awf/_proc.py``, the ``_forbid_session_kill`` tripwire in
   ``tests/conftest.py``, and the regression test.
3. ``diff`` — files changed vs ``BASELINE-<todo>.sha`` (list +
   insertions/deletions), cross-checked with the TODO contract's ``files``
   list when declared.
4. ``reject_leak`` — RUN5 #1 failsafe: files of a REJECTED attempt
   (``REJECT-*.files``) that would be SILENTLY excluded from this unit's
   commit (still untracked AND in ``BASELINE-<todo>.untracked``) are listed
   as a WARNING with the fix (``carry_over_from``). A warning, not a
   verdict failure — the pack never commits anything.
5. ``untracked_excluded`` — RUN10 #4 (TODO-0074): the pre-existing
   untracked files (``BASELINE-<todo>.untracked``) that the commit gate
   will exclude from this unit's commit. Informational (status "pass"):
   the exclusion is the default commit isolation, nothing is lost — no
   fix advice.
6. ``qa_findings`` — TODO-0240: ``context/QA-<NNNN>-findings-test.py`` (the
   full ``QA-TODO-<NNNN>-…`` name is accepted too), when present, runs once
   as a pytest module; a green file passes, a red one fails the pack (the
   same weight as a contract test). No file → ``none`` (skipped); no pytest
   → an explicit section error.
7. ``contract_tests`` — commands from the TODO contract ``verify:`` block,
   each with a timeout (``run_tree``). Test-like commands (``pytest`` /
   ``py.test`` / ``python -m pytest``, including a venv path), also behind
   a runner prefix (``uv run``, ``poetry run``), are run twice:
   ``flake: ok (2/2)`` when both outcomes agree, ``flake: unstable`` +
   WARNING when they differ. The FIRST outcome decides the gate: pass then
   fail stays a pass (with the warning), fail then pass stays a FAIL — the
   red is never hidden. Every other command (``run-all``, coverage, mutation
   smoke, arbitrary shell) runs ONCE, as before; a timeout is not retried.
   The contract's ``flake: off`` disables the probe for the whole unit.
   Independently of ``flake``, a test-like command at rc 0 is judged by its
   canonical pytest summary: ``xfailed``/``skipped`` above the contract's
   ``xfail_allow``/``skip_allow`` (per command, 0 by default) fail it;
   ``xpassed`` is only a note.
8. ``prove_red`` — the contract's ``prove_red`` list through
    :func:`awf.prove_red.prove_red`; the verdict goes into the report. The
    mutually exclusive ``prove_red_pin`` list runs the same baseline check
    but for regression shields of EXISTING behavior: green on the baseline
    is ``pin-ok`` (a pass, the teeth are proven by mutation outside the
    tool), red on the baseline is a ``fail`` (a finding, not a pin), and a
    collection error is ``broken-runner``. The declared coverage kind
    (TODO-0201, ``prove_red_kind: coverage`` + ``prove_red_note``) makes a
    ``not-red`` verdict a pass (annotated, with the note in the report);
    an UNdeclared ``not-red`` on a tests-only diff gets a hint line
    suggesting the declaration — the verdict is never auto-substituted.
    The ``new-api`` kind (TODO-0231) makes a ``broken-runner`` whose typed
    cause is the missing new symbol (prove-red's ``new_code_acceptable``
    fact) a ``new-api-ok`` pass — a warning does not colour the exit code;
    a real broken runner keeps its fail.
9. ``candidate`` — R2.4: reads ``context/CANDIDATE-<todo>.json`` (written by
   the approve) and re-derives the bound facts (HEAD, contract, environment,
   candidate tree, degradation set) with the SAME validator the commit gate
   runs; ``skipped`` when the unit was not approved through the R2.4 path.
10. ``done_json`` — executor-declared facts from ``DONE-<todo>.json`` (U3),
    explicitly marked as executor data (unverified).
11. ``spec_budget`` — K6 reference: the six-document word table and the
    package total for a unit that declares ``spec_package``. Always
    ``pass`` and never a verdict failure: readiness lives in the three R3
    points, and a late edit must not kill finished work (D6).
12. ``lint`` — ``ruff check .``.

Verdict / exit code: 0 = every measured check passed, 1 = at least one
failure, 2 = nothing was measured at all (no baseline, no contract, no
gates configured). A section that was not run is ``skipped`` and does not
count as measured. The pack never raises for a broken optional input —
it notes the problem in the report instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import command_parse, done_owner, paths
from ._errors import AwfApiError
from ._log import log as _log
from ._proc import run_tree
from .infra.git_model import (
    GitModelError,
    GitRepository,
    display_path,
    read_untracked_snapshot,
)
from .infra.managed_fs import ManagedPathError, managed_write_text
from .prove_red import (
    VERDICT_BROKEN,
    VERDICT_GREEN_AFTER,
    VERDICT_NOT_RED,
    VERDICT_RED_OK,
    _looks_like_test_file,
    prove_red,
)
from .signals import short_id
from .unit_contract import parse_done_json, parse_todo_contract, render_done_json
from .verify import parse_argv

#: Per-gate subprocess timeout (seconds).
GATE_TIMEOUT = 300
#: Per-command timeout for contract ``verify:`` entries (seconds).
CMD_TIMEOUT = 600
#: ruff timeout (seconds).
LINT_TIMEOUT = 120

#: (short name, script path, precondition paths) — mirrors run-all.sh.
GATE_SCRIPTS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("contracts", "scripts/check-contracts.sh", ("docs/contracts",)),
    ("ratchet", "scripts/ratchet.sh", (".ratchet-counters",)),
    ("instructions", "scripts/instruction-gates.sh",
     ("scripts/instruction-gates.conf",)),
)

#: (label, file, needle) — the 2026-09-20 session-kill incident canaries.
SAFETY_CANARIES: tuple[tuple[str, str, str], ...] = (
    ("`pid > 1` guard in awf/_proc.py", "awf/_proc.py", "pid > 1"),
    ("`_forbid_session_kill` tripwire in tests/conftest.py",
     "tests/conftest.py", "_forbid_session_kill"),
)
SAFETY_REGRESSION_TEST = "test_kill_process_tree_never_targets_pid_1"

_TODO_ID_RE = re.compile(r"TODO-\d{4,}")


@dataclass
class Section:
    """One report section: status, detail lines, denominator note."""

    name: str
    status: str  # "pass" | "fail" | "skipped"
    lines: list[str] = field(default_factory=list)
    measured: bool = False
    detail: str = ""  # one-line summary for the JSON block


@dataclass
class VerifyPackResult:
    """Structured outcome of one verify-pack run."""

    todo_id: str
    verdict: str  # "ok" | "failed" | "nothing-measured"
    exit_code: int  # 0 / 1 / 2
    measured: int
    report_path: str = ""
    sections: dict[str, str] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


# ─── low-level runners (module level so tests can monkeypatch) ───────────


def _tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) <= n:
        return lines[-1]
    return " … ".join(lines[-n:])


def _run_cmd(
    cmd: str,
    cwd: Path,
    timeout: int,
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, bool]:
    """Run a command as argv (``shlex.split``), no shell.

    A4 (REVIEW виток 2): the same launch mechanism as the candidate check
    (:func:`awf.candidate_verify.run_check`) and verify.py — one command→
    argv rule (:func:`awf.verify.parse_argv`), so a command means the same
    thing on every surface it is gated by. An env prefix
    (``PYTHONPATH=... python -m pytest``) is SHELL syntax — pass the
    environment through ``env`` instead. Bad quoting and a launch failure
    (missing executable, permission denied — the old ``shell=True`` turned
    those into rc 127) fail typed (rc 127 + ``ABORTED`` text), never a
    traceback.

    Returns (rc, output, timed_out)."""
    parts, problem = parse_argv(cmd)
    if problem:
        return 127, f"ABORTED: {problem}", False
    try:
        cp = run_tree(
            parts,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, "", True
    except (subprocess.SubprocessError, OSError) as e:
        return 127, f"ABORTED: {e}", False
    out = (cp.stdout or "") + (("\n" + cp.stderr) if cp.stderr else "")
    return cp.returncode, out, False


def _run_lint(cwd: Path, timeout: int) -> tuple[int | None, str]:
    """ruff check cwd. Returns (None, "") when ruff is not installed."""
    if shutil.which("ruff") is None:
        return None, ""
    rc, out, _timed_out = _run_cmd("ruff check .", cwd, timeout)
    return rc, out


# ─── sections ────────────────────────────────────────────────────────────


def _gates_section(project: Path, timeout: int) -> Section:
    s = Section(name="gates", status="pass")
    ran: list[str] = []
    failed: list[str] = []
    for name, script, preconds in GATE_SCRIPTS:
        missing = [p for p in preconds if not (project / p).exists()]
        if missing or not (project / script).is_file():
            s.lines.append(f"- {name}: skipped — {', '.join(missing) or script} not found")
            continue
        rc, out, timed_out = _run_cmd(f"sh {script}", project, timeout)
        if timed_out:
            failed.append(name)
            s.lines.append(f"- {name}: FAIL — timeout after {timeout}s")
            continue
        (ran if rc == 0 else failed).append(name)
        s.lines.append(f"- {name}: {'pass' if rc == 0 else 'FAIL'} (rc={rc}) — {_tail(out)}")
    s.measured = bool(ran or failed)
    if failed:
        s.status = "fail"
        s.detail = f"{len(failed)} of {len(ran) + len(failed)} gates failed: {', '.join(failed)}"
    else:
        s.detail = f"{len(ran)} of {len(ran)} gates ok" if ran else "no gates configured"
    s.lines.append("full suite: см. QA (полный сьют в пакет не входит)")
    return s


def _safety_section(project: Path) -> Section:
    if not (project / "awf" / "_proc.py").is_file():
        return Section(
            name="safety",
            status="skipped",
            lines=["skipped — no awf/_proc.py (not an awf repo)"],
        )
    s = Section(name="safety", status="pass")
    checked = 0
    for label, rel, needle in SAFETY_CANARIES:
        f = project / rel
        if not f.is_file():
            s.status = "fail"
            s.lines.append(f"- {label}: FAIL — file missing")
            continue
        checked += 1
        if needle in f.read_text(encoding="utf-8", errors="replace"):
            s.lines.append(f"- {label}: pass")
        else:
            s.status = "fail"
            s.lines.append(f"- {label}: FAIL — '{needle}' not found")
    # Regression test present somewhere under tests/
    tests_dir = project / "tests"
    found_test = False
    if tests_dir.is_dir():
        for py in tests_dir.rglob("*.py"):
            try:
                if SAFETY_REGRESSION_TEST in py.read_text(encoding="utf-8", errors="replace"):
                    found_test = True
                    break
            except OSError:
                continue
    checked += 1
    if found_test:
        s.lines.append(f"- regression test {SAFETY_REGRESSION_TEST}: present")
    else:
        s.status = "fail"
        s.lines.append(f"- regression test {SAFETY_REGRESSION_TEST}: FAIL — not found under tests/")
    s.measured = True
    s.detail = f"{checked} canaries checked"
    return s


def _is_noise(rel: str) -> bool:
    """Bytecode noise a gate command (e.g. `import calc`) drops into the tree."""
    parts = rel.split("/")
    return "__pycache__" in parts or rel.endswith(".pyc")


def _git_lines(project: Path, *args: str) -> tuple[int, str]:
    try:
        cp = subprocess.run(
            ["git", *args],
            cwd=str(project),
            capture_output=True,
            text=True,
            errors="surrogateescape",
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as e:
        # FU-21 D3: a hung git (dead process holding index.lock, stalled
        # filesystem) used to escape as a raw TimeoutExpired traceback and
        # kill the whole verify pack. rc 124 is the conventional timeout
        # code; every caller already degrades rc != 0 into a failed
        # section, so hand it the timeout text.
        return 124, f"git {' '.join(args)} timed out after {e.timeout}s"
    return cp.returncode, (cp.stdout or "").strip()


def _git_paths(project: Path, rev: str) -> tuple[int, str, list[str], list[str]]:
    """Tracked-changed and untracked paths vs ``rev`` from the R2.1 model.

    M4 (TODO-0228, добор 0221): the printed ``--name-only`` /
    ``ls-files --others`` reads crashed the whole verify pack with a
    ``UnicodeDecodeError`` under ``core.quotePath=false`` + a non-UTF-8 name
    (text=True decodes strictly) and with ``OSError``/``SubprocessError`` on
    a hung git. The model reads the NUL protocol and hands back raw bytes,
    so a non-UTF-8 name is displayed, never fatal. A git refusal degrades
    to ``rc=1`` + its text — every caller already turns that into a failed
    or skipped section.
    """
    try:
        repo = GitRepository(project)
        tracked = [display_path(p) for p in repo.changed_paths(rev)]
        untracked = [display_path(p) for p in repo.untracked_paths()]
    except (GitModelError, OSError) as exc:
        return 1, str(exc), [], []
    return 0, "", tracked, untracked


def _diff_section(project: Path, todo_id: str, contract: dict | None) -> Section:
    baseline_file = paths.context_dir(project) / f"BASELINE-{todo_id}.sha"
    sha = ""
    if baseline_file.is_file():
        try:
            raw = baseline_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            # NEG-3: corrupt (non-UTF-8) baseline bytes — the diff cannot
            # be measured, skip the section instead of tracebacks.
            return Section(
                name="diff",
                status="skipped",
                lines=[
                    f"skipped — BASELINE-{todo_id}.sha unreadable "
                    "(corrupt bytes), diff not measured"
                ],
            )
        sha = raw.splitlines()[0] if raw else ""
    if not sha:
        return Section(
            name="diff",
            status="skipped",
            lines=[f"skipped — no BASELINE-{todo_id}.sha, diff not measured"],
        )
    rc, err, tracked_names, untracked_names = _git_paths(project, sha)
    if rc != 0:
        return Section(
            name="diff",
            status="fail",
            lines=[f"git diff vs {sha[:12]} failed: {err[:200]}"],
            measured=True,
            detail="git error",
        )
    untracked_set = set(untracked_names)
    # Files that were ALREADY untracked at the baseline are not this unit's work.
    # One reader for the fact: raw NUL form or the legacy printed form.
    old_untracked = read_untracked_snapshot(project, todo_id) or set()
    new_untracked = sorted(
        f for f in untracked_set - old_untracked if not _is_noise(f)
    )
    tracked_names = [f for f in tracked_names if not _is_noise(f)]
    changed = sorted(set(tracked_names) | set(new_untracked))

    rc, shortstat = _git_lines(project, "diff", "--shortstat", sha)
    n_files = n_ins = n_del = 0
    if rc == 0 and shortstat:
        m = re.search(r"(\d+) files?", shortstat)
        n_files = int(m.group(1)) if m else 0
        mi = re.search(r"(\d+) insertions?", shortstat)
        n_ins = int(mi.group(1)) if mi else 0
        md = re.search(r"(\d+) deletions?", shortstat)
        n_del = int(md.group(1)) if md else 0

    s = Section(
        name="diff",
        status="pass",
        lines=[
            f"baseline: {sha[:12]}",
            f"changed vs baseline: {n_files} tracked files, +{n_ins}/-{n_del}, "
            f"{len(new_untracked)} new untracked",
        ],
        measured=True,
        detail=f"{len(changed)} files ({n_files} tracked, {len(new_untracked)} new)",
    )
    for f in tracked_names:
        s.lines.append(f"- {f}")
    for f in new_untracked:
        s.lines.append(f"- (new) {f}")

    files_declared = None
    if contract:
        candidate = contract.get("files")
        if isinstance(candidate, list) and all(isinstance(x, str) for x in candidate):
            files_declared = [x.strip() for x in candidate if x.strip()]
    if files_declared:
        declared = set(files_declared)
        undeclared = [f for f in changed if f not in declared]
        declared_missing = sorted(declared - set(changed))
        if undeclared:
            s.status = "fail"
            s.detail = f"{len(undeclared)} changed file(s) not declared in the contract"
            s.lines.append("UNDECLARED (changed but not in the contract `files` list):")
            s.lines.extend(f"- {f}" for f in undeclared)
        elif declared_missing:
            s.lines.append(f"declared but unchanged: {', '.join(declared_missing)}")
        s.lines.append(f"cross-check vs contract `files` ({len(files_declared)} declared)")
    else:
        s.lines.append("no `files` list in the TODO contract — cross-check skipped")
    return s


def _reject_leak_section(project: Path, todo_id: str) -> Section:
    """RUN5 #1 (Part B): warn about rejected-attempt files that would be
    silently excluded from this unit's commit.

    A path is "orphaned" when it is listed in some ``REJECT-<origin>.files``,
    is still untracked, AND is in ``BASELINE-<todo_id>.untracked`` — the exact
    triple the commit gate uses to drop it from the commit. The section is a
    WARNING (status "pass", not "fail"): it surfaces the leak and the fix
    (``carry_over_from``) but never blocks or commits anything.
    """
    ctx = paths.context_dir(project)
    has_reject = ctx.is_dir() and any(ctx.glob("REJECT-*.files"))
    if not has_reject:
        return Section(
            name="reject_leak",
            status="skipped",
            lines=["skipped — no REJECT-*.files (nothing rejected with untracked work)"],
        )

    from .reject_files import orphaned_reject_files

    orphaned = orphaned_reject_files(project, todo_id)
    if not orphaned:
        return Section(
            name="reject_leak",
            status="pass",
            measured=True,
            lines=["no orphaned rejected-attempt files"],
            detail="no leak",
        )

    lines = [
        f"WARNING: files of a rejected attempt would be SILENTLY EXCLUDED from "
        f"the {todo_id} commit (still untracked AND in BASELINE-{todo_id}.untracked):"
    ]
    # R2.2 QA iter 4: the paths are RAW (byte-faithful) — escape them for the
    # report sink, so a name with a non-UTF-8 byte does not break the write.
    from .infra.git_model import escape_str_for_ui

    for origin, files in orphaned:
        for f in files:
            lines.append(f"- {escape_str_for_ui(f)}  (from {origin})")
    lines.append(
        "Fix: re-issue with carry_over_from=<origin> so the retry commit "
        "includes them, or commit them consciously. Nothing is auto-committed."
    )
    n = sum(len(f) for _o, f in orphaned)
    return Section(
        name="reject_leak",
        status="pass",
        measured=True,
        lines=lines,
        detail=f"WARNING: {n} orphaned file(s) would be excluded from the commit",
    )


def _untracked_excluded_section(project: Path, todo_id: str) -> Section:
    """RUN10 #4 (TODO-0074): list the pre-existing untracked files the
    commit gate will exclude from this unit's commit.

    Informational, modeled on ``reject_leak`` but WITHOUT the fix advice —
    the exclusion is the default commit isolation (changes since baseline
    only), so the status is always "pass" (or "skipped" when there is no
    baseline snapshot to measure against). Never a verdict failure.
    """
    baseline_set = read_untracked_snapshot(project, todo_id)
    if baseline_set is None:
        return Section(
            name="untracked_excluded",
            status="skipped",
            lines=[f"skipped — no BASELINE-{todo_id}.untracked snapshot"],
        )
    excluded = sorted(baseline_set)
    if not excluded:
        return Section(
            name="untracked_excluded",
            status="pass",
            measured=True,
            lines=["no pre-existing untracked files — nothing excluded from the commit"],
            detail="nothing excluded",
        )
    lines = [
        f"INFO: {len(excluded)} pre-existing untracked file(s) are EXCLUDED from "
        f"the {todo_id} commit (listed in BASELINE-{todo_id}.untracked — they "
        "existed before the baseline):"
    ]
    lines.extend(f"- {f}" for f in excluded)
    lines.append(
        "Default commit isolation (changes since baseline only) — nothing is lost."
    )
    return Section(
        name="untracked_excluded",
        status="pass",
        measured=True,
        lines=lines,
        detail=f"{len(excluded)} pre-existing untracked file(s) excluded from the commit",
    )


#: M1 (2б): a test-like contract command runs twice. The first run is the
#: canonical result; a differing second run marks the command as flaky.
FLAKE_RUNS = 2


def _is_test_like(cmd: str) -> bool:
    """True when a shell segment *invokes* pytest — so the probe doubles it.

    Directly (``pytest``, ``py.test``, possibly via an absolute venv path),
    as ``python -m pytest``, or through a runner prefix (``uv run pytest``,
    ``poetry run pytest``, ``env python -m pytest``). A wrapper (``coverage
    run -m pytest``, ``bash scripts/run-all.sh``, arbitrary shell) is NOT
    test-like and keeps its single run (R1). The parsing itself is shared
    with the suite gate and ``scripts/suite-canonical.sh``
    (``awf.command_parse``, AUD-27-3), so the three cannot drift again. A
    single ``&`` splits here (CTX3): ``echo x & pytest tests/`` runs pytest
    as a separate command, and the flake probe must double it; the suite
    gate keeps its named blind spot on ``&``, so the flag is per caller.
    """
    for segment in command_parse.split_segments(cmd, split_single_amp=True):
        program = command_parse.parse_words(command_parse.split_words(segment))
        if program is not None and program.kind in ("pytest", "python-pytest"):
            return True
    return False


def _qa_findings_path(project: Path, todo_id: str) -> Path | None:
    """The QA-finding test file, both accepted names (TODO-0240 review V1).

    The canonical name drops the ``TODO-`` prefix (``QA-0228-…`` — the form
    every live file uses, :func:`awf.signals.short_id` is the one prefix
    rule); the full id (``QA-TODO-0228-…``) stays accepted for callers that
    pass it verbatim. The canonical name wins when both exist.
    """
    short = short_id(todo_id)
    names = [f"QA-{short}-findings-test.py"]
    full = f"QA-{todo_id}-findings-test.py"
    if full not in names:
        names.append(full)
    context_dir = paths.context_dir(project)
    for name in names:
        path = context_dir / name
        if path.is_file():
            return path
    return None


_QA_PASSED_RE = re.compile(r"\b(\d+)\s+passed\b")


#: R4: the pytest summary words a contract command is judged by. Plural
#: forms map onto the singular key so ``1 error`` / ``2 errors`` agree.
_PYTEST_COUNT_WORDS = {
    "passed": "passed",
    "failed": "failed",
    "error": "error",
    "errors": "error",
    "xfailed": "xfailed",
    "xpassed": "xpassed",
    "skipped": "skipped",
    "deselected": "deselected",
    "warning": "warning",
    "warnings": "warning",
}
_PYTEST_COUNT_PAIR_RE = re.compile(
    r"\b(\d+)\s+(" + "|".join(_PYTEST_COUNT_WORDS) + r")\b"
)
#: ANSI SGR escapes: pytest emits the summary coloured when ``PY_COLORS`` /
#: ``FORCE_COLOR`` is inherited or the command passes ``--color=yes``. The
#: escape's final ``m`` is a word character, so the ``\b`` in the pair
#: pattern would miss the FIRST pair of a coloured line (``…[1m1 xfailed``)
#: and the whole gate would silently pass — strip colour before parsing.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _pytest_counts(output: str) -> dict[str, int]:
    """R4: parse the LAST pytest summary line into ``word -> count``.

    The canonical run's outcome is the one the gate already judged by rc;
    its pytest summary sits on the last line carrying ``<number> <word>``
    pairs (``1 failed, 2 passed in 0.12s``). A missing word is simply
    absent — callers read it as 0. Output without a summary (a non-test
    command, an aborted launch) yields ``{}``.
    """
    plain = _ANSI_ESCAPE_RE.sub("", output or "")
    for line in reversed(plain.splitlines()):
        pairs = _PYTEST_COUNT_PAIR_RE.findall(line)
        if not pairs:
            continue
        counts: dict[str, int] = {}
        for number, word in pairs:
            key = _PYTEST_COUNT_WORDS[word.lower()]
            counts[key] = counts.get(key, 0) + int(number)
        return counts
    return {}


def _allow_value(contract: dict | None, key: str) -> int:
    """R4: a contract allowance, 0 when absent (validation lives in the parser)."""
    value = (contract or {}).get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _xfail_skip_judgement(
    output: str, xfail_allow: int, skip_allow: int
) -> tuple[bool, str]:
    """R4: judge the canonical summary — ``(passed, note)``.

    ``passed`` is False when the summary shows more ``xfailed``/``skipped``
    than the unit declared; ``note`` then carries the numbers. Otherwise
    the note names the allowed counts (only when an allowance is declared)
    and any ``xpassed`` (stale xfail — never judged, A12).
    """
    counts = _pytest_counts(output)
    xfailed = counts.get("xfailed", 0)
    skipped = counts.get("skipped", 0)
    xpassed = counts.get("xpassed", 0)
    over = []
    if xfailed > xfail_allow:
        over.append(f"{xfailed} xfailed > xfail_allow {xfail_allow}")
    if skipped > skip_allow:
        over.append(f"{skipped} skipped > skip_allow {skip_allow}")
    if over:
        return False, "FAIL — " + ", ".join(over)
    notes = []
    if xfail_allow or skip_allow:
        notes.append(f"allowed {xfailed} xfailed, {skipped} skipped")
    if xpassed:
        notes.append(f"{xpassed} xpassed (stale xfail)")
    return True, "; ".join(notes)


def _summary_signature(rc: int | None, output: str) -> tuple:
    """R4/AC22: the comparable outcome of a TEST-LIKE run.

    The flake repeat exists only for test-like commands, so this helper
    compares the judged counts on top of rc: ``0 xfailed`` turning into
    ``1 xfailed`` between the canonical run and the repeat is a visible
    instability even when both exit 0 — the verdict still comes from the
    first run. Non-test commands never reach here (they run once).
    """
    counts = _pytest_counts(output)
    return (rc, counts.get("xfailed", 0), counts.get("skipped", 0))


def _pytest_timeout_available() -> bool:
    """True when the ``pytest-timeout`` plugin loads in this interpreter.

    AA2 (TODO-0240 review v7): the plugin lives in the ``dev`` extra, but
    the verify pack runs in the RUNTIME environment — a machine with pytest
    and without the plugin would answer ``--timeout`` with rc=4
    ("unrecognized arguments") and turn a GREEN QA file into a confirmed
    finding. The per-command wall-clock timeout in ``_run_cmd``
    (``run_tree`` kills the process group) stays in force either way, so
    the flag is an optimisation, not the guard.
    """
    return importlib.util.find_spec("pytest_timeout") is not None


def _pytest_reports_passed(output: str) -> bool:
    """True when the pytest summary carries at least one passed test (X3)."""
    return any(int(n) > 0 for n in _QA_PASSED_RE.findall(output))


def _qa_findings_section(project: Path, todo_id: str, timeout: int) -> Section:
    """TODO-0240: run ``context/QA-<NNNN>-findings-test.py`` when it exists.

    The file is the supervisor's reproduction test for a QA finding — a
    pytest MODULE (``test_*`` functions), not a bare script: it runs through
    ``pytest`` ONCE (no flake probe), and its outcome carries the same
    weight as a contract command — a red finding fails the pack. No file →
    ``none`` (skipped, not measured); pytest itself being unavailable is an
    explicit section error, never a pass.
    """
    path = _qa_findings_path(project, todo_id)
    if path is None:
        return Section(
            name="qa_findings",
            status="skipped",
            lines=["none — no QA-<NNNN>-findings-test.py"],
            detail="none",
        )
    py = shlex.quote(sys.executable)
    # Z1: the QA file lives outside ``tests/``, so the suite's conftest (and
    # its session-kill tripwire) is not loaded for it. Load the packaged
    # tripwire as a plugin and make that package importable regardless of
    # where awf was installed — an unguarded natural repro such as
    # ``os.killpg(1, 15)`` would signal the whole user session.
    pkg_parent = Path(__file__).resolve().parent.parent
    # A4 (REVIEW виток 2): the env prefix (``PYTHONPATH=... ${PYTHONPATH:…}``)
    # is shell syntax — with the argv launcher it moves into the explicit
    # environment, appended after any inherited value exactly like the old
    # ``${PYTHONPATH:+:$PYTHONPATH}`` expansion did.
    qa_env = dict(os.environ)
    inherited = os.environ.get("PYTHONPATH")
    qa_env["PYTHONPATH"] = (
        f"{pkg_parent}{':' + inherited if inherited else ''}"
    )
    timeout_arg = (
        f"--timeout={int(timeout)} " if _pytest_timeout_available() else ""
    )
    cmd = (
        f"{py} -m pytest {shlex.quote(str(path))} -q "
        f"{timeout_arg}-p no:cacheprovider "
        "-p awf._safety_tripwire"
    )
    rc, out, timed_out = _run_cmd(cmd, project, timeout, env=qa_env)
    if "No module named pytest" in out:
        return Section(
            name="qa_findings",
            status="fail",
            lines=[f"- `{path.name}`: ERROR — pytest is not available in {sys.executable}"],
            measured=True,
            detail="pytest-missing",
        )
    if timed_out:
        return Section(
            name="qa_findings",
            status="fail",
            lines=[f"- `{path.name}`: FAIL — timeout after {timeout}s"],
            measured=True,
            detail="timeout",
        )
    ok = rc == 0
    if ok and not _pytest_reports_passed(out):
        # An all-skip/xfail module exits 0 without proving anything — a
        # finding is only disproved by a test that actually ran (X3).
        return Section(
            name="qa_findings",
            status="fail",
            lines=[
                f"- `{path.name}`: FAIL — no tests executed "
                f"(no 'passed' in the pytest output) — {_tail(out)}"
            ],
            measured=True,
            detail="no tests executed",
        )
    return Section(
        name="qa_findings",
        status="pass" if ok else "fail",
        lines=[
            f"- `{path.name}`: {'pass' if ok else f'FAIL (rc={rc})'} — {_tail(out)}"
        ],
        measured=True,
        detail="pass" if ok else f"fail (rc={rc})",
    )


def _contract_tests_section(project: Path, contract: dict | None, timeout: int) -> Section:
    if isinstance(contract, _BrokenContract):
        # F2 (REVIEW виток 3): the block exists but does not parse — the
        # gate FAILS with the reason ("skipped — no block" would hide a
        # declared verification entirely; "broken runner" is what the
        # supervisor would need to see to re-materialize the contract).
        return Section(
            name="contract_tests",
            status="fail",
            lines=[
                f"- the TODO has a contract block, but it does not parse: "
                f"{contract.reason} — fix the block (front-matter YAML/"
                "`verify:` keys) and rerun the pack"
            ],
            detail=f"fail — broken contract block: {contract.reason}",
        )
    cmds = (contract or {}).get("verify")
    if not isinstance(cmds, list) or not cmds:
        return Section(
            name="contract_tests",
            status="skipped",
            lines=["skipped — no `verify:` block in the TODO contract"],
        )
    flake_value = (contract or {}).get("flake", "on")
    flake_off = flake_value is False or str(flake_value).strip().lower() == "off"
    xfail_allow = _allow_value(contract, "xfail_allow")
    skip_allow = _allow_value(contract, "skip_allow")
    s = Section(name="contract_tests", status="pass")
    ok = 0
    unstable = 0
    for cmd in cmds:
        command = str(cmd)
        test_like = _is_test_like(command)
        probe = not flake_off and test_like
        rc, out, timed_out = _run_cmd(command, project, timeout)
        if timed_out:
            # A timeout is never retried: the first run already failed.
            s.status = "fail"
            s.lines.append(f"- `{cmd}`: FAIL — timeout after {timeout}s")
            continue
        # R4: a test-like command at rc 0 still has to prove it ran the
        # tests — xfail/skip above the declared allowance is not green. The
        # CANONICAL (first) run decides; the flake repeat never overrides.
        summary_ok, summary_note = (
            _xfail_skip_judgement(out, xfail_allow, skip_allow)
            if test_like and rc == 0
            else (True, "")
        )
        suffix = f"; {summary_note}" if summary_note else ""
        if not probe:
            if rc != 0:
                s.status = "fail"
                s.lines.append(f"- `{cmd}`: FAIL (rc={rc}) — {_tail(out)}")
            elif not summary_ok:
                s.status = "fail"
                s.lines.append(f"- `{cmd}`: {summary_note}")
            else:
                ok += 1
                s.lines.append(f"- `{cmd}`: pass{suffix}")
            continue
        first_rc = rc
        repeats = [
            _run_cmd(command, project, timeout)
            for _ in range(FLAKE_RUNS - 1)
        ]
        same_outcome = all(
            not repeat_timeout
            and _summary_signature(repeat_rc, repeat_out)
            == _summary_signature(first_rc, out)
            for repeat_rc, repeat_out, repeat_timeout in repeats
        )
        # FLAKE_RUNS == 2 → exactly one repeat; the wording below calls it
        # "the second run". The constant stays the source of the count.
        second_rc, _second_out, second_timeout = (
            repeats[0] if repeats else (first_rc, "", False)
        )
        seen = "timeout" if second_timeout else f"rc={second_rc}"
        if not second_timeout and second_rc == first_rc == 0:
            seen += ", summary differs"
        if first_rc == 0:
            if same_outcome:
                flake_line = f"flake: ok ({FLAKE_RUNS}/{FLAKE_RUNS})"
            elif summary_ok:
                # The canonical run passed, so the differing repeat is a
                # WARNING only.
                flake_line = (
                    f"flake: unstable — WARNING: first rc=0, second {seen} — "
                    f"flakiness alone does not fail the gate"
                )
            else:
                # The canonical summary already failed: do not claim that
                # flakiness alone is irrelevant right next to a FAIL.
                flake_line = (
                    f"flake: unstable — WARNING: first rc=0, second {seen} — "
                    f"the canonical (first) summary decides"
                )
            if not summary_ok:
                # R4: the verdict is the canonical summary's, but the repeat
                # is still described honestly — a differing second summary
                # must not be reported as "flake: ok".
                s.status = "fail"
                if not same_outcome:
                    unstable += 1
                s.lines.append(f"- `{cmd}`: {summary_note}; {flake_line}")
                continue
            ok += 1
            if same_outcome:
                s.lines.append(f"- `{cmd}`: pass{suffix}; {flake_line}")
            else:
                # The first (canonical) outcome passed: the gate stays green,
                # the differing second run is a visible WARNING.
                unstable += 1
                s.lines.append(
                    f"- `{cmd}`: pass{suffix} (first outcome used); "
                    f"{flake_line}"
                )
            continue
        s.status = "fail"
        if same_outcome:
            s.lines.append(
                f"- `{cmd}`: FAIL (rc={first_rc}) — {_tail(out)}; "
                f"flake: ok ({FLAKE_RUNS}/{FLAKE_RUNS})"
            )
        elif second_rc == 0 and not second_timeout:
            unstable += 1
            s.lines.append(
                f"- `{cmd}`: FAIL (rc={first_rc}) — {_tail(out)}; "
                f"flake: unstable — WARNING: second run passed "
                f"(rc=0); first outcome used, the red is not hidden"
            )
        else:
            unstable += 1
            s.lines.append(
                f"- `{cmd}`: FAIL (rc={first_rc}) — {_tail(out)}; "
                f"flake: unstable — WARNING: second run {seen}; "
                f"first outcome used"
            )
    s.measured = True
    s.detail = f"{ok} of {len(cmds)} commands passed"
    if unstable:
        s.detail += (
            f", {unstable} unstable (WARNING; the first outcome decided "
            f"each gate)"
        )
    return s


def _changed_paths(project: Path, todo_id: str) -> list[str] | None:
    """The unit's changed paths vs its baseline — mirrors the ``diff``
    section: tracked changes (modified or deleted) + new untracked, minus
    the pre-baseline untracked and bytecode noise. None when the diff
    cannot be measured (no baseline, an unreadable baseline file, or a
    git error)."""
    baseline_file = paths.context_dir(project) / f"BASELINE-{todo_id}.sha"
    sha = ""
    if baseline_file.is_file():
        try:
            raw = baseline_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None
        sha = raw.splitlines()[0] if raw else ""
    if not sha:
        return None
    rc, _err, names, untracked_names = _git_paths(project, sha)
    if rc != 0:
        return None
    untracked_set = set(untracked_names)
    old_untracked = read_untracked_snapshot(project, todo_id) or set()
    changed = {f for f in names if f and not _is_noise(f)}
    changed |= {f for f in untracked_set - old_untracked if not _is_noise(f)}
    return sorted(changed)


def _coverage_hint(project: Path, todo_id: str) -> str | None:
    """TODO-0201: the auto-detect hint for an UNDECLARED not-red.

    The unit's diff is non-empty and every changed path looks like a test
    file (so no source was touched): a coverage unit by construction,
    where not-red is expected. A hint line for the GATES report only —
    the verdict is NOT altered (no auto-substitution)."""
    changed = _changed_paths(project, todo_id)
    if not changed or not all(_looks_like_test_file(f) for f in changed):
        return None
    return (
        "hint: the diff is tests-only (no source changed) and the verdict "
        "is not-red — this looks like a coverage unit; declare "
        "`prove_red_kind: coverage` + `prove_red_note:` in the TODO "
        "contract"
    )


def _prove_red_section(
    project: Path,
    todo_id: str,
    contract: dict | None,
    tmp_base: str | Path | None,
) -> Section:
    contract = contract or {}
    # The contract forbids both at once; a pin takes the same section slot
    # with shield semantics (green on the baseline is a pass, not a fail).
    if contract.get("prove_red_pin"):
        return _prove_red_pin_section(project, todo_id, contract, tmp_base)
    tests = contract.get("prove_red")
    if not isinstance(tests, list) or not tests:
        return Section(
            name="prove_red",
            status="skipped",
            lines=["skipped — no `prove_red:` block in the TODO contract"],
        )
    try:
        res = prove_red(project, todo_id, tests=list(tests), tmp_base=tmp_base)
    except AwfApiError as e:
        return Section(
            name="prove_red",
            status="fail",
            lines=[f"prove-red could not run: {e}"],
            measured=True,
            detail="error",
        )
    lines = [
        f"verdict: {res.verdict} (exit {res.exit_code}) — baseline {res.baseline_sha[:12]}",
        res.message,
    ]
    for w in res.warnings:
        lines.append(f"warning: {w}")
    kind = contract.get("prove_red_kind")
    # TODO-0201/TODO-0231: a declared kind turns EXACTLY ONE verdict into a
    # pass with its justification; every other verdict stays a fail (the
    # declaration exempts nothing else). ``new-api`` passes a broken-runner
    # whose typed cause is the missing new symbol (prove-red's
    # ``new_code_acceptable`` fact — never the warning's prose); a broken
    # runner WITHOUT that fact (collection error outside the project, 0
    # tests, timeout) keeps its fail. ``coverage`` passes a not-red: the
    # tests guard already-implemented behavior, so they are green on this
    # unit's baseline by construction.
    declared: tuple[str, str] | None = None
    if kind == "new-api" and res.verdict == VERDICT_BROKEN and res.new_code_acceptable:
        declared = (
            "new-api-ok — the test falls on the baseline only because the "
            "new symbol does not exist yet (declared `prove_red_kind: "
            "new-api`). This is a pass with a warning, not a red.",
            "new-api-ok",
        )
    elif kind == "coverage" and res.verdict == VERDICT_NOT_RED:
        declared = (
            "coverage unit — not-red is expected (declared "
            "`prove_red_kind: coverage`): the tests guard "
            "already-implemented behavior, so they are green on this "
            "unit's baseline by construction.",
            "coverage-ok (declared not-red)",
        )
    if declared is not None:
        annotation, detail = declared
        lines.append(annotation)
        note = contract.get("prove_red_note")
        if isinstance(note, str) and note.strip():
            lines.append(f"note: {note.strip()}")
        return Section(
            name="prove_red",
            status="pass",
            lines=lines,
            measured=True,
            detail=detail,
        )
    # An undeclared not-red keeps its fail; a tests-only diff suggests the
    # unit is coverage (a hint line only, the verdict is untouched).
    if res.verdict == VERDICT_NOT_RED and kind is None:
        hint = _coverage_hint(project, todo_id)
        if hint:
            lines.append(hint)
    status = "pass" if res.verdict == VERDICT_RED_OK else "fail"
    return Section(
        name="prove_red",
        status=status,
        lines=lines,
        measured=True,
        detail=f"verdict {res.verdict}",
    )


def _prove_red_pin_section(
    project: Path,
    todo_id: str,
    contract: dict,
    tmp_base: str | Path | None,
) -> Section:
    """The ``prove_red_pin:`` check — a shield of EXISTING behavior (REPORTS29).

    A pin must be GREEN on the baseline (that is the invariant: it guards
    behavior that already holds); its teeth — that it would actually catch a
    regression — are proven by mutation, outside this tool. So the baseline
    outcome maps to shield verdicts, NOT to the red-before-fix ones:

    - ``not-red`` (green on the baseline) -> ``pin-ok`` (pass, with a note);
    - ``red-ok``/``green-after`` (red on the baseline) -> ``fail`` — a
      finding: the test falls on the old code, so it is not a shield;
    - ``broken-runner`` (collection error / 0 tests) -> ``broken-runner``.
    """
    tests = contract.get("prove_red_pin")
    if not isinstance(tests, list) or not tests:
        return Section(
            name="prove_red",
            status="skipped",
            lines=["skipped — no `prove_red_pin:` block in the TODO contract"],
        )
    try:
        res = prove_red(project, todo_id, tests=list(tests), tmp_base=tmp_base)
    except AwfApiError as e:
        return Section(
            name="prove_red",
            status="fail",
            lines=[f"prove-red-pin could not run: {e}"],
            measured=True,
            detail="error",
        )
    base = res.baseline_sha[:12]
    if res.verdict == VERDICT_NOT_RED:  # green on the baseline
        status, detail = "pass", "pin-ok"
        lines = [
            f"verdict: pin-ok (exit {res.exit_code}) — baseline {base}",
            "regression shield: GREEN on the baseline, as a pin must be. The "
            "teeth (that it would catch a regression) are proven by mutation, "
            "outside this tool.",
        ]
    elif res.verdict in (VERDICT_RED_OK, VERDICT_GREEN_AFTER):  # red on the baseline
        status, detail = "fail", "fail"
        lines = [
            f"verdict: fail (exit {res.exit_code}) — baseline {base}",
            "NOT A PIN: the test is RED on the baseline — it falls on the old "
            "code, so it is not a shield of existing behavior. This is a "
            "finding (a broken test), not a pin.",
        ]
    elif res.verdict == VERDICT_BROKEN:  # collection error / 0 tests
        status, detail = "fail", "broken-runner"
        lines = [
            f"verdict: broken-runner (exit {res.exit_code}) — baseline {base}",
            "collection error / 0 tests: the pin could not be run on the "
            "baseline — the check did not run.",
        ]
    else:  # runner-unsupported or anything unexpected
        status, detail = "fail", res.verdict
        lines = [f"verdict: {res.verdict} (exit {res.exit_code}) — baseline {base}", res.message]
    if res.baseline_output:
        lines.append(_tail(res.baseline_output))
    return Section(
        name="prove_red",
        status=status,
        lines=lines,
        measured=True,
        detail=detail,
    )


# ─── A8: DONE.json claims are checked, not trusted ──────────────────────

#: How many claimed commands one check runs. A report lists a handful; a
#: pathological list must not turn verify into a second full suite.
CLAIM_MAX_COMMANDS = 5
#: Per-command budget for a claim check (the worker's own budget is 600).
#: Owner decision (TODO-0321, A8): 120 s per command and 600 s for the whole
#: check — five claims at 300 s each kept verify blocked for 25 minutes.
CLAIM_TIMEOUT = 120
#: The whole check, all commands together. A claim that does not fit is
#: "not checked", never a contradiction.
CLAIM_TOTAL_TIMEOUT = 600

_SHELL_OPERATORS = ("&&", "||", ";", "|", ">", "<", "`", "$(")


def _counted(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


#: The closed set of skip reasons, each with its wording: ``skip()`` refuses a
#: reason missing here, so a new call site cannot reach the report unworded.
_SKIP_WORDS: dict[str, tuple[str, str]] = {
    "limit": ("claim beyond the limit", "claims beyond the limit"),
    "shell": ("shell line", "shell lines"),
    "claim": ("claim that is not green", "claims that are not green"),
    "malformed": ("malformed entry", "malformed entries"),
    "empty": ("entry without a command", "entries without a command"),
    "budget": ("claim beyond the time budget", "claims beyond the time budget"),
    # A27: a line of prose ("reused canonical suite artifact …") is not a
    # command. Rerunning it produced FileNotFoundError and a false
    # contradiction; it belongs in the skipped half, named as what it is.
    "prose": ("line that is not a command", "lines that are not commands"),
    # REVIEW-0337: an auditor probe is a demonstration in a directory the
    # auditor built, not a claim about this project. It is rerun only when
    # it names an existing `cwd`; the rest are named, never executed.
    "probe": ("probe not rerun (demonstration)", "probes not rerun (demonstrations)"),
}


@dataclass
class ClaimsReport:
    """A8 result: contradictions, rerun count and what stayed unchecked.

    AUD4: a claim that outlived ``timeout`` is ``unverified`` — an honest
    "not checked in time", never a contradiction. AUD6: the counts make the
    unchecked half visible instead of silent.

    ``checked`` counts every claim the rerun actually answered (green or
    red); a contradicted claim is checked too, it just also lands in
    ``mismatches``. A claim the check never ran stays out of the count.
    """

    mismatches: list[str] = field(default_factory=list)
    checked: int = 0
    unverified: list[str] = field(default_factory=list)
    #: reason → count, in the order the check names them.
    skipped: dict[str, int] = field(default_factory=dict)
    # A27: auditor probes live in PROBES-<id>.json and are counted apart from
    # the author's DONE claims — a reader must see which file said what.
    probe_mismatches: list[str] = field(default_factory=list)
    probes_checked: int = 0
    probes_unverified: list[str] = field(default_factory=list)
    probes_skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str, *, probes: bool = False) -> None:
        """Count one unchecked claim; ``reason`` must have a wording above."""
        if reason not in _SKIP_WORDS:
            raise KeyError(f"no wording for the skip reason {reason!r}")
        bucket = self.probes_skipped if probes else self.skipped
        bucket[reason] = bucket.get(reason, 0) + 1

    def summary(self) -> str:
        parts = [f"checked {self.checked}"]
        if self.skipped:
            parts.append(
                "skipped: "
                + ", ".join(
                    _counted(count, *_SKIP_WORDS[reason])
                    for reason, count in self.skipped.items()
                )
            )
        if self.unverified:
            parts.append(_counted(len(self.unverified), "claim not verified in time",
                                  "claims not verified in time"))
        return "claims — " + "; ".join(parts)

    def probes_summary(self) -> str:
        parts = [f"checked {self.probes_checked}"]
        if self.probes_skipped:
            parts.append(
                "skipped: "
                + ", ".join(
                    _counted(count, *_SKIP_WORDS[reason])
                    for reason, count in self.probes_skipped.items()
                )
            )
        if self.probes_unverified:
            parts.append(
                _counted(
                    len(self.probes_unverified),
                    "probe not verified in time",
                    "probes not verified in time",
                )
            )
        return "auditor probes — " + "; ".join(parts)

    #: True when the unit carries a ``PROBES-<id>.json`` at all — the report
    #: names the file even when it holds no rerun-able claim.
    probes_present: bool = False

    def has_probes(self) -> bool:
        """Did the check see a PROBES file at all?"""
        return self.probes_present

    def lines(self) -> list[str]:
        out = [f"A8: {self.summary()}"]
        out.extend(f"  - {line}" for line in self.unverified)
        if self.has_probes():
            out.append(f"A8: {self.probes_summary()}")
            out.extend(f"  - {line}" for line in self.probes_unverified)
            demonstrations = self.probes_skipped.get("probe", 0)
            if demonstrations:
                # REVIEW-0337: a probe without an existing `cwd` is an
                # auditor's demonstration, not a claim about this project.
                out.append(
                    "A8: auditor probes: "
                    f"{_counted(demonstrations, 'entry', 'entries')} "
                    "not rerun (demonstrations)"
                )
        return out


def _claimed_green(result: str) -> bool | None:
    """Did the worker claim a GREEN run? ``None`` when the text is unclear."""
    text = (result or "").strip().lower()
    if not text:
        return None
    if any(word in text for word in ("fail", "error", "traceback", "rc=1", "rc 1")):
        return False
    if any(
        word in text
        for word in ("passed", "pass", "ok", "green", "rc=0", "success")
    ):
        return True
    return None


def check_done_claims(
    project_dir: str | Path,
    todo_id: str,
    *,
    timeout: int = CLAIM_TIMEOUT,
    limit: int = CLAIM_MAX_COMMANDS,
    total_timeout: int = CLAIM_TOTAL_TIMEOUT,
) -> ClaimsReport:
    """A8: rerun the commands ``DONE-<todo>.json`` says the worker ran.

    A27: ``PROBES-<todo>.json`` (the reviewer/auditor's own file) is read
    apart from the author's facts — one file never overwrites the other.
    REVIEW-0337: a probe is evidence, not a claim about this repository:
    it is rerun only when it names an existing ``cwd``; the rest are
    counted as demonstrations and never executed from the repository root.

    Returns a report: one line per CONTRADICTION (a claimed green whose
    command now fails), the number of claims rerun, the ones that outlived
    the budget (``unverified`` — AUD4: not a contradiction) and the ones
    that stayed unchecked, by reason (AUD6: the counts are part of the
    result, not silent).

    The point is narrow: a report may not say "12 passed" about a command
    that does not pass. Everything a worker asserts beyond that stays a
    declaration — verify-pack renders it as such.
    """
    report = ClaimsReport()
    project = Path(project_dir).resolve()
    outbox = paths.outbox(project)

    done_path = done_owner.done_path(outbox, todo_id)
    probes_path = done_owner.probes_path(outbox, todo_id)
    entries = _claims_entries(done_path)
    probes = _claims_entries(probes_path)
    report.probes_present = probes is not None
    if entries is None and probes is None:
        return report

    # AUD4 (owner decision): the total budget belongs to the check, not to
    # the worker. A command that no longer fits is not run at all — the
    # report says so instead of spending the last seconds on a doomed try.
    # A27: both files share one deadline — the check is still one check.
    deadline = time.monotonic() + total_timeout
    if entries is not None:
        _rerun_claims(
            entries, project, report,
            timeout=timeout, limit=limit, deadline=deadline, probes=False,
        )
    if probes is not None:
        _rerun_claims(
            probes, project, report,
            timeout=timeout, limit=limit, deadline=deadline, probes=True,
        )
    return report


def _claims_entries(path: Path | None) -> list | None:
    """``tests_run`` of one claims file, or None when the file is unusable."""
    if path is None:
        return None
    try:
        data = parse_done_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    entries = data.get("tests_run") or []
    if not isinstance(entries, list):
        return None
    return entries


def _is_command(token: str, project: Path) -> bool:
    """Is the first token a program, or a word from a sentence?

    A path must exist; a bare name must be on PATH. A27: anything else is
    prose. Running it raised FileNotFoundError and the report called an
    honest line a lie.
    """
    looks_like_path = "/" in token or token.startswith((".", "~"))
    if looks_like_path:
        candidate = Path(token).expanduser()
        resolved = candidate if candidate.is_absolute() else project / candidate
        # A folder is not a program: `exists()` let a prose line naming a
        # directory reach `subprocess.run`, and the PermissionError became a
        # contradiction the rerun never made. Same predicate as the shell
        # gates' interpreter check (`-f`), for the same reason.
        return resolved.is_file()
    return shutil.which(token) is not None


def _probe_cwd(entry: dict, project: Path) -> Path | None:
    """The directory an auditor probe runs in, or None for a demonstration.

    REVIEW-0337: a probe is often a command run inside a scratch project the
    auditor assembled ("doctor --project-dir /tmp/…"). Rerunning it from the
    repository root contradicts a claim nobody made. The optional ``cwd``
    makes a probe reproducible: an existing directory means "run it there",
    anything else keeps the probe out of the rerun — named, not executed.
    """
    cwd = entry.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    candidate = Path(cwd).expanduser()
    if not candidate.is_absolute():
        candidate = project / candidate
    return candidate if candidate.is_dir() else None


def _rerun_claims(
    entries: list,
    project: Path,
    report: ClaimsReport,
    *,
    timeout: int,
    limit: int,
    deadline: float,
    probes: bool,
) -> None:
    """Rerun one claims file into ``report`` (its own counters when probes)."""
    beyond = max(0, len(entries) - limit)
    for _ in range(beyond):
        report.skip("limit", probes=probes)

    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            report.skip("malformed", probes=probes)
            continue
        cmd = str(entry.get("cmd") or "").strip()
        claimed = _claimed_green(str(entry.get("result") or ""))
        if not cmd:
            report.skip("empty", probes=probes)
            continue
        if claimed is not True:
            report.skip("claim", probes=probes)
            continue
        try:
            argv = shlex.split(cmd)
        except ValueError:
            report.skip("malformed", probes=probes)
            continue
        if not argv:
            report.skip("empty", probes=probes)
            continue
        # A27: a leading assignment (``FOO=1 pytest``) is a shell line — the
        # assignment is the shell's syntax, not a program. Skipping it as
        # prose would misname it; rerunning the raw argv fails with
        # FileNotFoundError and looks like a contradiction.
        if command_parse.is_assignment(argv[0]):
            report.skip("shell", probes=probes)
            continue
        # argv only: a bare operator TOKEN means a shell line. A `;` inside
        # quotes (`python3 -c 'import sys; sys.exit(1)'`) is one argument,
        # not a pipeline — the split already removed the quotes.
        if any(token in _SHELL_OPERATORS for token in argv):
            report.skip("shell", probes=probes)
            continue
        # REVIEW-0337: an auditor probe runs in its own directory when it
        # names one. Without an existing `cwd` it is a demonstration — the
        # repository root is the wrong place to rerun it.
        cwd = project
        if probes:
            probe_dir = _probe_cwd(entry, project)
            if probe_dir is None:
                report.skip("probe", probes=True)
                continue
            cwd = probe_dir
        # A27: prose is not a command. A line like "reused canonical suite
        # artifact …" names a fact, not an argv — it is skipped, not rerun.
        if not _is_command(argv[0], cwd):
            report.skip("prose", probes=probes)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            report.skip("budget", probes=probes)
            continue
        # The smaller of the two: one claim may not eat the whole check.
        budget = min(timeout, remaining)
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=budget,
                check=False,
            )
            rc = done.returncode
        except subprocess.TimeoutExpired:
            # AUD4: the budget is the check's, not the worker's — a claim
            # that did not fit says so and stays out of the failure count.
            if probes:
                report.probes_unverified.append(f"not verified in {budget:g}s: {cmd}")
            else:
                report.unverified.append(f"not verified in {budget:g}s: {cmd}")
            continue
        except (OSError, subprocess.SubprocessError) as e:
            _record_mismatch(
                report, probes,
                f"claimed {entry.get('result')!r} but the command could not "
                f"run ({type(e).__name__}: {e}): {cmd}",
            )
            continue
        if probes:
            report.probes_checked += 1
        else:
            report.checked += 1
        if rc != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()
            extra = f" — {tail[-1][:160]}" if tail else ""
            _record_mismatch(
                report, probes,
                f"claimed {entry.get('result')!r} but rc={rc}: {cmd}{extra}",
            )


def _record_mismatch(report: ClaimsReport, probes: bool, line: str) -> None:
    if probes:
        report.probe_mismatches.append(line)
    else:
        report.mismatches.append(line)


def _claims_lines(report: ClaimsReport) -> tuple[list[str], bool]:
    """The A8 part of the done_json section, and whether a probe failed.

    One assembly for both callers — a unit with an author's DONE and an
    audit-only unit with PROBES alone — so the same facts cannot be
    worded two ways in one pack.
    """
    lines: list[str] = []
    if (
        report.mismatches or report.checked or report.skipped
        or report.unverified or report.has_probes()
    ):
        lines.append("")
        lines.extend(report.lines())
    if report.mismatches or report.probe_mismatches:
        lines.append("A8: the claims below do not survive a rerun:")
        lines.extend(f"  - {line}" for line in report.mismatches)
        lines.extend(f"  - probe: {line}" for line in report.probe_mismatches)
    return lines, bool(report.probe_mismatches)


def _done_json_section(project: Path, todo_id: str, logs_dir: Path) -> Section:
    path = done_owner.done_path(paths.outbox(project), todo_id)
    if path is None:
        # A27: an audit-only unit has no author DONE, but its PROBES still
        # hold claims — those are checked, and named, without inventing a
        # DONE section.
        report = check_done_claims(project, todo_id)
        lines = [f"skipped — no DONE-{todo_id}.json in outbox"]
        claims, probe_failed = _claims_lines(report)
        lines.extend(claims)
        if probe_failed:
            return Section(
                name="done_json",
                status="fail",
                lines=lines,
                measured=True,
                detail="an auditor probe does not reproduce",
            )
        return Section(
            name="done_json",
            status="skipped",
            lines=lines,
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # NEG-3: binary garbage in the DONE json degrades to a skipped
        # section, the same as an unreadable file.
        return Section(
            name="done_json",
            status="skipped",
            lines=[f"skipped — {path.name} unreadable ({e})"],
        )
    data = parse_done_json(text)
    if data is None:
        _log(logs_dir, f"U5: {path.name} is broken or violates the DONE.json schema — section skipped")
        return Section(
            name="done_json",
            status="skipped",
            lines=[f"skipped — {path.name} is broken or violates the schema"],
        )
    lines = ["executor data (declared by the implementer):"]
    lines.extend(render_done_json(data))
    report = check_done_claims(project, todo_id)
    claims, _ = _claims_lines(report)
    lines.extend(claims)
    if report.mismatches or report.probe_mismatches:
        return Section(
            name="done_json",
            status="fail",
            lines=lines,
            measured=True,
            detail="claimed green run does not reproduce",
        )
    return Section(
        name="done_json",
        status="pass",
        lines=lines,
        measured=False,
        detail="executor facts included; claimed green commands reproduce",
    )


def _checklist_section(project: Path, todo_id: str) -> Section:
    """TODO-0244: the Definition-of-done gate as a pack section.

    Present block → run/reuse the gate (sha-pinned); a missing item fails
    the section. No block → ``checklist: none`` and skipped — old units
    without the block keep working exactly as before.
    """
    from .checklist_gate import ChecklistGateError
    from .checklist_gate import run as run_gate
    from .checklists import ChecklistError

    try:
        result = run_gate(project, todo_id)
    except ChecklistGateError as e:
        # Not the same fact as "the unit carries no block": the gate could
        # not read the unit at all. Keep it visibly distinct — a silent
        # `none` here would read as "nothing to check".
        return Section(
            name="checklist",
            status="skipped",
            lines=[f"checklist: unavailable — {e}"],
            detail="unavailable",
        )
    except ChecklistError as e:
        # The unit HAS a block and the block is broken (a hand-edited line,
        # a refused probe argv). That is a failed check, not "nothing to
        # check" — and it must be a SECTION verdict, never a pack traceback.
        # ``measured`` follows every other failing section: a failure was
        # actually observed, so the report counts it as measured.
        return Section(
            name="checklist",
            status="fail",
            lines=[f"checklist: fail — {e}"],
            measured=True,
            detail="fail",
        )
    except (OSError, RuntimeError) as e:
        # A filesystem error on a probe target (an unreadable file, a
        # symlink loop) is the same kind of fact as a broken block: the
        # check FAILED to run. It must stay a section verdict — a traceback
        # out of the pack hides every other section.
        return Section(
            name="checklist",
            status="fail",
            lines=[f"checklist: fail — {type(e).__name__}: {e}"],
            measured=True,
            detail="fail",
        )
    if result["status"] == "none":
        return Section(
            name="checklist",
            status="skipped",
            lines=["checklist: none — the TODO carries no `## Чек-лист завершения` block"],
            detail="none",
        )
    s = Section(name="checklist", status="pass", measured=True)
    for item in result["items"]:
        mark = {"ok": "[x]", "missing": "[ ]", "manual": "[~]"}[item["status"]]
        line = f"- {mark} {item['id']}: {item['text']}"
        if item["detail"]:
            line += f" — {item['detail']}"
        s.lines.append(line)
    if result.get("cached"):
        s.lines.append(f"cache: reused for tree {result['tree_sha'][:12]}")
    for warning in result.get("warnings", []):
        s.lines.append(f"warning: {warning}")
    if result["missing"]:
        s.status = "fail"
        s.detail = "missing: " + ", ".join(result["missing"])
        s.lines.append("missing: " + ", ".join(result["missing"]))
    elif result["manual"]:
        s.detail = f"{len(result['manual'])} manual item(s), probes ok"
    else:
        s.detail = "all probes ok"
    return s


def _package_budget_line(total: int, limit: int) -> str:
    """K6: the package total carries the same MAJOR marker as a document."""
    mark = "MAJOR" if total > limit else "ok"
    return f"- пакет: {total} слов (порог {limit}) — {mark}"


def _spec_budget_section(project: Path, contract: dict | None) -> Section:
    """SPEC IMP8 (D6): the package volume budget as a REFERENCE.

    Prints the six-document word table and the package total. It never
    fails the pack and never blocks a commit: the real readiness check
    lives in the three R3 points (dispatch, start, continue). Calling
    ``spec_gate`` here would re-check readiness on verify — exactly what
    D6 forbids (a late edit would kill finished work).
    """
    if isinstance(contract, _BrokenContract):
        return Section(
            name="spec_budget",
            status="skipped",
            lines=[f"skipped — TODO contract block is broken: {contract.reason}"],
            detail="broken contract",
        )
    if not isinstance(contract, dict):
        return Section(
            name="spec_budget",
            status="skipped",
            lines=["skipped — no TODO contract block"],
        )
    raw = contract.get("spec_package")
    if not isinstance(raw, str) or not raw.strip():
        return Section(
            name="spec_budget",
            status="skipped",
            lines=["skipped — the unit declares no `spec_package`"],
        )
    from . import config as awf_config
    from .spec_package import SpecError, budget_limits, budget_rows

    package = Path(raw)
    if not package.is_absolute():
        package = project / package
    try:
        document_limit, package_limit = budget_limits(awf_config.load(project))
        rows = budget_rows(package, document_limit=document_limit)
    except (SpecError, OSError) as e:
        return Section(
            name="spec_budget",
            status="skipped",
            lines=[f"skipped — package unreadable: {e}"],
            detail="unreadable",
        )
    total = sum(row.words for row in rows)
    s = Section(name="spec_budget", status="pass")
    for row in rows:
        mark = "MAJOR" if row.over else "ok"
        s.lines.append(
            f"- {row.path}: {row.words} слов (порог {row.limit}) — {mark}"
        )
    s.lines.append(_package_budget_line(total, package_limit))
    s.detail = f"{total} words, {len(rows)} docs"
    return s


def _lint_section(project: Path, timeout: int) -> Section:
    rc, out = _run_lint(project, timeout)
    if rc is None:
        return Section(
            name="lint",
            status="skipped",
            lines=["skipped — ruff not found on PATH"],
        )
    if rc == 0:
        return Section(
            name="lint",
            status="pass",
            lines=["- `ruff check .`: pass"],
            measured=True,
            detail="ruff clean",
        )
    return Section(
        name="lint",
        status="fail",
        lines=[f"- `ruff check .`: FAIL (rc={rc}) — {_tail(out)}"],
        measured=True,
        detail="ruff found problems",
    )


# ─── candidate binding (R2.4) ────────────────────────────────────────────


def _candidate_section(project: Path, todo_id: str) -> Section:
    """R2.4: the candidate evidence binding (§ scope: binding, not gates).

    Reads ``context/CANDIDATE-<todo>.json`` (written by the approve) and
    re-derives the bound facts from the current tree — the same validator
    the commit gate runs. Missing evidence = skipped (the unit was not
    approved through the R2.4 path); a mismatch is a FAIL naming the moved
    fact (HEAD/contract/candidate/environment).
    """
    from . import candidate_verify

    evidence = candidate_verify.read_evidence(project, todo_id)
    if evidence is None:
        return Section(
            name="candidate",
            status="skipped",
            lines=[
                f"skipped — no CANDIDATE-{todo_id}.json (the unit was not "
                "approved through the R2.4 candidate path)"
            ],
        )
    from . import commit_plan

    baseline = commit_plan.read_baseline_sha(project, todo_id)
    refusal = candidate_verify.validate_evidence(
        evidence, project, todo_id, baseline
    )
    head = "pass" if not refusal else "fail"
    lines = [
        f"- candidate_tree: `{evidence.candidate_tree or '(empty)'}`",
        f"- check: {evidence.check_status}",
        f"- generation: {evidence.generation}",
        f"- degraded: {list(evidence.degraded) or 'none'}",
    ]
    if refusal:
        lines.append(f"- INVALIDATED: {refusal}")
    return Section(
        name="candidate",
        status=head,
        lines=lines,
        measured=True,
        detail="candidate binding valid" if not refusal else "candidate binding invalidated",
    )


# ─── TODO contract loading ───────────────────────────────────────────────


class _BrokenContract(dict):
    """F2 (REVIEW виток 3): the TODO HAS a contract block, but it does not
    parse — the opposite of "no block", it must not be rendered as one.

    Behaves as an empty contract for every ``.get()`` reader (diff/prove-red
    skip their declared keys, the same as for a missing block); only
    ``_contract_tests_section`` checks the type and FAILS the gate with the
    parse reason instead of skipping it with "no block".
    """

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


def _load_todo_contract(project: Path, todo_id: str, logs_dir: Path) -> dict | None:
    """Read the TODO (inbox, or the archive) and parse its contract block.

    Returns None when there is NO block at all (the pack must not crash on
    it); a block that does not parse returns :class:`_BrokenContract`
    (F2) so the gate still reports the TODO's own declared commands — the
    fact it exists and refuses to parse is never silently "skipped".
    """
    candidates = (
        paths.inbox(project) / f"{todo_id}.md",
        paths.done_dir(project) / todo_id / "TODO.md",
    )
    for f in candidates:
        if f.is_file():
            try:
                # T1: surrogateescape — a non-UTF-8 TODO is legitimate input
                # (byte-faithful text, the same tolerance as the unified
                # reader ``todos.read_todo_text``); a strict decode crashed
                # the whole pack before any section or report was written.
                text = f.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError as e:
                _log(logs_dir, f"U5: {f} unreadable ({e}) — no contract")
                return None
            try:
                contract, unknown = parse_todo_contract(text)
            except ValueError as e:
                reason = f"contract block in {f} is broken: {e}"
                _log(logs_dir, f"U5: {reason}")
                return _BrokenContract(reason)
            if unknown:
                _log(logs_dir, f"U5: unknown contract keys in {f}: {', '.join(unknown)}")
            return contract
    return None


# ─── report ──────────────────────────────────────────────────────────────


def _current_tree_sha(project: Path) -> str:
    """The content-addressed tree fingerprint ``awf tree-sha`` reports.

    Best effort: a non-git project or a missing binary leaves the field
    empty — the reader then cannot compare and says so instead of guessing.
    A hung git call is the same fact: ``tree_fingerprint`` talks to git with
    a 30 s timeout, and its ``TimeoutExpired`` must not escape into the
    report renderer — that would turn a real verdict into the engine's
    crash note (the fingerprint is a marker, not a gate).
    """
    from . import git_utils

    try:
        return git_utils.tree_fingerprint(project)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""


#: ``## Verdict: **failed** (exit 1) — 8 section(s) measured``
_VERDICT_RE = re.compile(r"^## Verdict: \*\*([^*]+)\*\*", re.MULTILINE)
#: ``## 3. diff: PASS`` — the report's section headings in document order.
_PACK_SECTION_RE = re.compile(
    r"^##\s+\d+\.\s+(\S+):\s+(PASS|FAIL|SKIPPED)\s*$", re.MULTILINE
)
#: ``- `<cmd>`: FAIL (rc=1) — tail`` — a measured failure, never a heading.
_FAIL_MARKER_RE = re.compile(r"^- .*: FAIL \(rc=\d+\)", re.MULTILINE)
_PACK_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def read_gates_facts(
    path: str | Path, *, tree_sha: str | None = None
) -> dict | None:
    """K4/AC26–AC29: the verify-pack verdict as a fact, not a log.

    Reads the same ``GATES-<id>.md`` module :func:`verify_pack` writes: the
    markdown for section order and the failure marker, the JSON block for
    the machine fields. When the block is missing or broken, the headings
    still supply ``sections``/``failed_sections`` and the marker still
    supplies ``first_failure`` — the reason never depends on the JSON alone.
    No second parser, no logs.

    Args:
        path: the report file.
        tree_sha: current tree fingerprint; when passed, ``stale`` compares
            it with the one recorded in the pack. Omitted, ``stale`` stays
            ``False`` — "not compared", not "fresh": a caller that renders
            freshness MUST pass it.

    Returns:
        ``None`` when the file does not exist. Otherwise a dict with
        ``verdict`` (``ok``/``failed``/``nothing-measured``/``error``/
        ``unreadable``), ``exit_code``, ``measured``, ``sections``,
        ``details``, ``report``, ``first_failure``, ``note``,
        ``generated``, ``tree_sha`` and ``stale``.

    A crashed engine leaves an ``**error**`` note without JSON (no traceback
    is lost, but there is nothing to parse): that is ``error`` with the note
    text, not ``missing``. A file with neither a JSON block nor the error
    heading is ``unreadable`` — an empty dict would read as "nothing failed".
    """
    report = Path(path)
    if not report.is_file():
        return None
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    verdict_match = _VERDICT_RE.search(text)
    markdown_verdict = verdict_match.group(1).strip().lower() if verdict_match else ""

    facts: dict = {
        "verdict": "unreadable",
        "exit_code": None,
        "measured": None,
        "sections": {},
        "details": {},
        "failed_sections": [],
        "report": str(report),
        "first_failure": "",
        "note": "",
        "generated": "",
        "tree_sha": "",
        "stale": False,
    }

    json_match = _PACK_JSON_RE.search(text)
    payload: dict = {}
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            payload = parsed

    if payload:
        facts["verdict"] = str(
            payload.get("verdict") or markdown_verdict or "unreadable"
        ).strip().lower()
        exit_code = payload.get("exit_code")
        facts["exit_code"] = exit_code if isinstance(exit_code, int) else None
        measured = payload.get("measured")
        facts["measured"] = measured if isinstance(measured, int) else None
        if isinstance(payload.get("sections"), dict) and payload["sections"]:
            facts["sections"] = {
                str(k): str(v).lower() for k, v in payload["sections"].items()
            }
        else:
            # A hand-edited/truncated block must not hide the headings the
            # markdown still carries.
            facts["sections"] = _markdown_sections(text)
        if isinstance(payload.get("details"), dict):
            facts["details"] = {
                str(k): str(v) for k, v in payload["details"].items()
            }
        if isinstance(payload.get("report"), str):
            facts["report"] = payload["report"]
        facts["tree_sha"] = str(payload.get("tree_sha") or "")
    elif markdown_verdict == "error":
        # The engine's crash note (pipeline_engine._write_verify_pack_error_note):
        # a verdict heading, prose instead of JSON. Keep the verdict and the
        # note text — "the pack did not run" is a fact, not an absence.
        facts["verdict"] = "error"
        facts["note"] = _first_note_line(text)
    else:
        facts["verdict"] = (markdown_verdict or "unreadable").strip().lower()
        facts["sections"] = _markdown_sections(text)

    # The markdown owns these facts whatever the JSON block says: a missing
    # or broken block must not take the reason and the generation time down
    # with it (AC27, D6).
    generated_match = re.search(r"^Generated:\s*(.+)$", text, re.MULTILINE)
    facts["generated"] = generated_match.group(1).strip() if generated_match else ""
    facts["failed_sections"] = _failed_section_names(text, facts["sections"])
    facts["first_failure"] = _first_fail_marker(text)

    if tree_sha is not None:
        facts["stale"] = bool(facts["tree_sha"] != tree_sha)
    return facts


def _markdown_sections(text: str) -> dict[str, str]:
    """Section name → status from the headings, in document order.

    The fallback when the JSON block is absent or carries no ``sections``:
    the headings are the same report, and a reader that returned ``{}``
    would make a failed pack look like one with nothing measured.
    """
    return {
        m.group(1): m.group(2).lower() for m in _PACK_SECTION_RE.finditer(text)
    }


def _failed_section_names(text: str, sections: dict) -> list[str]:
    """Names of the ``fail`` sections, in document order.

    The JSON object keeps the writer's order, but the markdown is the
    reader's contract for "which one was first" — a reordered or
    hand-edited block must not reorder the reasons.
    """
    ordered = [m.group(1) for m in _PACK_SECTION_RE.finditer(text)]
    if not ordered:
        ordered = list(sections)
    return [
        name
        for name in ordered
        if str(sections.get(name, "")).lower() == "fail"
    ]


def _first_fail_marker(text: str) -> str:
    """The first ``- <cmd>: FAIL (rc=N)`` inside the first failing section.

    AC27: a section heading (``## 1. contracts: FAIL``) is not a reason, and
    a later section never wins. No marker at all (a failed section whose
    detail has another shape) leaves the reason empty rather than inventing
    one.
    """
    headings = list(_PACK_SECTION_RE.finditer(text))
    for i, match in enumerate(headings):
        if match.group(2) != "FAIL":
            continue
        start = match.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        for line in text[start:end].splitlines():
            if _FAIL_MARKER_RE.match(line):
                return line.strip()
        return ""
    return ""


def _first_note_line(text: str) -> str:
    """The first non-empty line after the verdict heading of a crash note."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _VERDICT_RE.match(line):
            for candidate in lines[i + 1:]:
                if candidate.strip():
                    return candidate.strip()
            break
    return ""


def _render_report(
    project: Path,
    todo_id: str,
    sections: list[Section],
    verdict: str,
    exit_code: int,
    measured: int,
) -> str:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    parts: list[str] = [
        f"# GATES-{todo_id} — verify-pack report",
        "",
        f"Generated: {ts}",
        f"Project: {project.name}",
        f"Todo: {todo_id}",
        "",
        f"## Verdict: **{verdict}** (exit {exit_code}) — {measured} section(s) measured",
        "",
    ]
    for i, s in enumerate(sections, 1):
        mark = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIPPED"}[s.status]
        parts.append(f"## {i}. {s.name}: {mark}")
        parts.extend(s.lines)
        parts.append("")
    payload = {
        "todo_id": todo_id,
        "verdict": verdict,
        "exit_code": exit_code,
        "measured": measured,
        "sections": {s.name: s.status for s in sections},
        "details": {s.name: s.detail for s in sections if s.detail},
        "report": f".agentic/context/GATES-{todo_id}.md",
        # IMP3 (D6): the tree the pack was taken on — the reader compares it
        # with the current tree to tell a fresh verdict from yesterday's.
        "tree_sha": _current_tree_sha(project),
    }
    parts += ["---", "", "```json", json.dumps(payload, indent=2, ensure_ascii=False), "```", ""]
    return "\n".join(parts)


# ─── entry point ─────────────────────────────────────────────────────────


def verify_pack(
    project_dir: str | Path,
    todo_id: str,
    *,
    write_report: bool = True,
    report_path: str | Path | None = None,
    tmp_base: str | Path | None = None,
    gate_timeout: int = GATE_TIMEOUT,
    cmd_timeout: int = CMD_TIMEOUT,
    lint_timeout: int = LINT_TIMEOUT,
) -> VerifyPackResult:
    """Run the deterministic verify pack and write ``GATES-<todo>.md``.

    Args:
        project_dir: Project root (must contain ``.agentic/``).
        todo_id: TODO identifier (e.g. "TODO-0022") — selects the baseline.
        write_report: False keeps the report in memory only (tests).
        report_path: Override the report location.
        tmp_base: Base dir for prove-red worktrees (tests pass tmp_path).
        gate_timeout / cmd_timeout / lint_timeout: per-command timeouts.

    Returns:
        VerifyPackResult (verdict, exit code 0/1/2, per-section statuses).

    Raises:
        AwfApiError: bad todo_id or no ``.agentic/`` in the project.
    """
    project = Path(project_dir).resolve()
    if not _TODO_ID_RE.fullmatch(todo_id):
        raise AwfApiError(f"bad todo_id {todo_id!r} — expected TODO-NNNN")
    agentic = paths.agentic_dir(project)
    if not agentic.is_dir():
        raise AwfApiError(f"{project} is not an awf project (no .agentic/)")
    logs_dir = agentic / "logs"

    contract = _load_todo_contract(project, todo_id, logs_dir)

    sections = [
        _gates_section(project, gate_timeout),
        _safety_section(project),
        _diff_section(project, todo_id, contract),
        _reject_leak_section(project, todo_id),
        _untracked_excluded_section(project, todo_id),
        _qa_findings_section(project, todo_id, cmd_timeout),
        _contract_tests_section(project, contract, cmd_timeout),
        _prove_red_section(project, todo_id, contract, tmp_base),
        _candidate_section(project, todo_id),
        _done_json_section(project, todo_id, logs_dir),
        _checklist_section(project, todo_id),
        _spec_budget_section(project, contract),
        _lint_section(project, lint_timeout),
    ]

    measured = sum(1 for s in sections if s.measured)
    failed = [s.name for s in sections if s.status == "fail"]
    if measured == 0:
        verdict, exit_code = "nothing-measured", 2
    elif failed:
        verdict, exit_code = "failed", 1
    else:
        verdict, exit_code = "ok", 0

    report_path = Path(report_path) if report_path else (
        paths.context_dir(project) / f"GATES-{todo_id}.md"
    )
    report_str = _render_report(project, todo_id, sections, verdict, exit_code, measured)
    if write_report:
        # R1.1/F-A: the default report sits under ``.agentic/context`` — a
        # symlinked ``.agentic``/``context`` must not send it outside the
        # project. An explicit out-of-tree ``report_path`` is the caller's
        # own choice and keeps the plain write.
        try:
            report_path.relative_to(project)
        except ValueError:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                report_str, encoding="utf-8", errors="backslashreplace"
            )
        else:
            try:
                managed_write_text(
                    report_path,
                    report_str,
                    root=project,
                    errors="backslashreplace",
                )
            except (ManagedPathError, OSError):
                # Reporting is best-effort: an unsafe runtime path must not
                # crash the verify pack (the verdict is already computed).
                pass

    return VerifyPackResult(
        todo_id=todo_id,
        verdict=verdict,
        exit_code=exit_code,
        measured=measured,
        report_path=str(report_path),
        sections={s.name: s.status for s in sections},
        details={s.name: s.detail for s in sections if s.detail},
    )


__all__ = [
    "GATE_SCRIPTS",
    "SAFETY_CANARIES",
    "Section",
    "VerifyPackResult",
    "read_gates_facts",
    "verify_pack",
]
