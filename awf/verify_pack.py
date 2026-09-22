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
5. ``contract_tests`` — commands from the TODO contract ``verify:`` block,
   each with a timeout (``run_tree``).
6. ``prove_red`` — the contract's ``prove_red`` list through
   :func:`awf.prove_red.prove_red`; the verdict goes into the report.
7. ``done_json`` — executor-declared facts from ``DONE-<todo>.json`` (U3),
   explicitly marked as executor data (unverified).
8. ``lint`` — ``ruff check .``.

Verdict / exit code: 0 = every measured check passed, 1 = at least one
failure, 2 = nothing was measured at all (no baseline, no contract, no
gates configured). A section that was not run is ``skipped`` and does not
count as measured. The pack never raises for a broken optional input —
it notes the problem in the report instead.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from ._errors import AwfApiError
from ._log import log as _log
from ._proc import run_tree
from .prove_red import VERDICT_RED_OK, prove_red
from .unit_contract import parse_done_json, parse_todo_contract, render_done_json

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
) -> tuple[int | None, str, bool]:
    """Run a shell command. Returns (rc, output, timed_out)."""
    try:
        cp = run_tree(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            shell=True,
        )
    except subprocess.TimeoutExpired:
        return None, "", True
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


def _diff_section(project: Path, todo_id: str, contract: dict | None) -> Section:
    baseline_file = paths.context_dir(project) / f"BASELINE-{todo_id}.sha"
    sha = ""
    if baseline_file.is_file():
        raw = baseline_file.read_text(encoding="utf-8").strip()
        sha = raw.splitlines()[0] if raw else ""
    if not sha:
        return Section(
            name="diff",
            status="skipped",
            lines=[f"skipped — no BASELINE-{todo_id}.sha, diff not measured"],
        )
    rc, names = _git_lines(project, "diff", "--name-only", sha)
    if rc != 0:
        return Section(
            name="diff",
            status="fail",
            lines=[f"git diff vs {sha[:12]} failed: {names[:200]}"],
            measured=True,
            detail="git error",
        )
    rc, untracked = _git_lines(project, "ls-files", "--others", "--exclude-standard")
    untracked_set = set(untracked.splitlines()) if rc == 0 and untracked else set()
    # Files that were ALREADY untracked at the baseline are not this unit's work.
    old_untracked_file = paths.context_dir(project) / f"BASELINE-{todo_id}.untracked"
    old_untracked: set[str] = set()
    if old_untracked_file.is_file():
        old_untracked = {
            ln.strip() for ln in old_untracked_file.read_text(encoding="utf-8").splitlines() if ln.strip()
        }
    new_untracked = sorted(
        f for f in untracked_set - old_untracked if not _is_noise(f)
    )
    tracked_names = [f for f in names.splitlines() if not _is_noise(f)]
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
    for origin, files in orphaned:
        for f in files:
            lines.append(f"- {f}  (from {origin})")
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


def _contract_tests_section(project: Path, contract: dict | None, timeout: int) -> Section:
    cmds = (contract or {}).get("verify")
    if not isinstance(cmds, list) or not cmds:
        return Section(
            name="contract_tests",
            status="skipped",
            lines=["skipped — no `verify:` block in the TODO contract"],
        )
    s = Section(name="contract_tests", status="pass")
    ok = 0
    for cmd in cmds:
        rc, out, timed_out = _run_cmd(str(cmd), project, timeout)
        if timed_out:
            s.status = "fail"
            s.lines.append(f"- `{cmd}`: FAIL — timeout after {timeout}s")
        elif rc == 0:
            ok += 1
            s.lines.append(f"- `{cmd}`: pass")
        else:
            s.status = "fail"
            s.lines.append(f"- `{cmd}`: FAIL (rc={rc}) — {_tail(out)}")
    s.measured = True
    s.detail = f"{ok} of {len(cmds)} commands passed"
    return s


def _prove_red_section(
    project: Path,
    todo_id: str,
    contract: dict | None,
    tmp_base: str | Path | None,
) -> Section:
    tests = (contract or {}).get("prove_red")
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
    status = "pass" if res.verdict == VERDICT_RED_OK else "fail"
    lines = [
        f"verdict: {res.verdict} (exit {res.exit_code}) — baseline {res.baseline_sha[:12]}",
        res.message,
    ]
    for w in res.warnings:
        lines.append(f"warning: {w}")
    return Section(
        name="prove_red",
        status=status,
        lines=lines,
        measured=True,
        detail=f"verdict {res.verdict}",
    )


def _done_json_section(project: Path, todo_id: str, logs_dir: Path) -> Section:
    path = paths.outbox(project) / f"DONE-{todo_id}.json"
    if not path.is_file():
        return Section(
            name="done_json",
            status="skipped",
            lines=[f"skipped — no DONE-{todo_id}.json in outbox"],
        )
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
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
    lines = ["executor data (unverified — what the implementer declared):"]
    lines.extend(render_done_json(data))
    return Section(
        name="done_json",
        status="pass",
        lines=lines,
        measured=False,
        detail="executor facts included",
    )


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


# ─── TODO contract loading ───────────────────────────────────────────────


def _load_todo_contract(project: Path, todo_id: str, logs_dir: Path) -> dict | None:
    """Read the TODO (inbox, or the archive) and parse its contract block.

    Returns None when there is no block; a broken block is noted in the log
    and treated as absent — the pack must not crash on it.
    """
    candidates = (
        paths.inbox(project) / f"{todo_id}.md",
        paths.done_dir(project) / todo_id / "TODO.md",
    )
    for f in candidates:
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8")
            except OSError as e:
                _log(logs_dir, f"U5: {f} unreadable ({e}) — no contract")
                return None
            try:
                contract, unknown = parse_todo_contract(text)
            except ValueError as e:
                _log(logs_dir, f"U5: contract block in {f} is broken: {e}")
                return None
            if unknown:
                _log(logs_dir, f"U5: unknown contract keys in {f}: {', '.join(unknown)}")
            return contract
    return None


# ─── report ──────────────────────────────────────────────────────────────


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
        _contract_tests_section(project, contract, cmd_timeout),
        _prove_red_section(project, todo_id, contract, tmp_base),
        _done_json_section(project, todo_id, logs_dir),
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
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_str, encoding="utf-8")

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
    "verify_pack",
]
