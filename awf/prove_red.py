"""U4: ``awf prove-red`` — machine proof that a test is red before the fix.

"Red before the fix" used to rest on the implementer's word. This module
makes the machine prove it: the baseline commit is deployed into a
temporary ``git worktree`` under /tmp/opencode, the test files (new,
untracked ones included) are copied there from the current tree, and the
tests are run against the OLD code. A real red means an assertion failure
— a test that merely imports a not-yet-existing symbol proves nothing.

Verdicts (CLI exit codes):

- ``red-ok`` (0) — baseline run failed with a real red (collected tests > 0,
  no collection errors) and the same tests pass in the current tree;
- ``not-red`` (1) — tests PASSED on the baseline: they prove nothing;
- ``broken-runner`` (2) — 0 tests collected, collection error, broken
  runner, or the failure is only a missing-symbol import (expected for
  new code — a warning is attached);
- ``green-after`` (1) — red on the baseline, but the tests do not pass in
  the current tree either.

The worktree is removed in ``finally`` on every outcome
(``git worktree remove --force`` + ``git worktree prune``).

Hermeticity: the baseline run is launched through a small bootstrap
script injected into the worktree that first scrubs PEP 660 editable
meta-path finders and duplicate sys.path providers of the project's own
top-level packages. Without this, an editable install of the project in
site-packages silently leaks the CURRENT tree into the baseline run
(imports of modules missing from the worktree resolve to the installed
copy), which fakes a red.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import paths
from ._errors import AwfApiError
from ._proc import run_tree
from .unit_contract import parse_todo_contract

#: CLI exit codes, one per verdict family.
EXIT_RED_OK = 0
EXIT_NOT_RED = 1
EXIT_BROKEN = 2

VERDICT_RED_OK = "red-ok"
VERDICT_NOT_RED = "not-red"
VERDICT_BROKEN = "broken-runner"
VERDICT_GREEN_AFTER = "green-after"

#: Default base directory for temporary worktrees (SPEC-2: /tmp/opencode/**).
DEFAULT_TMP_BASE = "/tmp/opencode"

#: Filename of the hermeticity bootstrap injected into the baseline worktree.
_BOOTSTRAP_NAME = "_awf_prove_red_bootstrap.py"

#: Per-test timeout passed to pytest inside both runs.
PYTEST_TEST_TIMEOUT = 60
#: Whole pytest run must finish within this (run_tree kills the group after).
PYTEST_RUN_TIMEOUT = 600

#: Top-level names that are never project code — an import error naming one
#: of these is a broken environment, not a missing project symbol.
_NON_PROJECT_TOP_LEVELS = frozenset(
    {
        "os", "sys", "re", "json", "subprocess", "pathlib", "shutil",
        "typing", "dataclasses", "collections", "functools", "itertools",
        "math", "time", "datetime", "abc", "io", "copy", "enum", "glob",
        "hashlib", "hmac", "base64", "binascii", "codecs", "struct",
        "tempfile", "unittest", "pytest", "yaml", "jinja2", "markdown",
        "mcp", "fastapi", "uvicorn",
    }
)


@dataclass
class ProveRedResult:
    """Structured outcome of one prove-red run."""

    todo_id: str
    verdict: str
    exit_code: int
    baseline_sha: str
    tests: list[str]
    copied_files: list[str]
    baseline_output: str
    current_output: str
    message: str
    warnings: list[str] = field(default_factory=list)
    worktree: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def prove_red(
    project_dir: str | Path,
    todo_id: str,
    tests: list[str] | None = None,
    *,
    tmp_base: str | Path | None = None,
) -> ProveRedResult:
    """Prove that ``tests`` are red on the baseline sha and green now.

    Args:
        project_dir: Project root (must be a git repo with .agentic/).
        todo_id: TODO identifier (e.g. "TODO-0021") — selects the baseline.
        tests: Test files and/or ``file::test`` ids. Default: the
            ``prove_red`` block of the TODO contract (U3).
        tmp_base: Base directory for the temporary worktree. Default
            ``/tmp/opencode`` (SPEC-2); tests pass their tmp_path.

    Returns:
        ProveRedResult with verdict, exit code and both pytest outputs.

    Raises:
        AwfApiError: bad todo_id, no .agentic/, not a git repo, missing or
            invalid baseline sha, or a test file absent from the tree.
    """
    project_dir = Path(project_dir).resolve()
    if not re.fullmatch(r"TODO-\d{4,}", todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}', expected format TODO-NNNN"
        )
    _require_agentic(project_dir)
    _require_git_repo(project_dir)

    if tests is None:
        tests = _tests_from_contract(project_dir, todo_id)
    if not tests:
        raise AwfApiError(
            f"no tests given and {todo_id} has no prove_red block in its "
            "contract — pass --tests explicitly (file paths and/or "
            "file::test ids)"
        )

    baseline_sha = _read_baseline_sha(project_dir, todo_id)

    worktree = _new_worktree(project_dir, baseline_sha, tmp_base)
    warnings: list[str] = []
    current_output = ""
    try:
        copied = _copy_test_files(project_dir, worktree, tests)
        baseline_rc, baseline_output = _run_pytest(worktree, tests, hermetic=True)
        classification = _classify_pytest(baseline_rc, baseline_output, project_dir)

        if classification == "passed":
            verdict, code = VERDICT_NOT_RED, EXIT_NOT_RED
            message = (
                f"NOT RED: the tests PASSED on baseline {baseline_sha[:12]} — "
                "they prove nothing. Rewrite them so the unfixed code fails."
            )
        elif classification == "real-red":
            current_rc, current_output = _run_pytest(project_dir, tests)
            if current_rc == 0:
                verdict, code = VERDICT_RED_OK, EXIT_RED_OK
                message = (
                    f"RED-OK: on baseline {baseline_sha[:12]} the tests fail "
                    "with a real red (assertions), and they pass in the "
                    "current tree."
                )
            else:
                verdict, code = VERDICT_GREEN_AFTER, EXIT_NOT_RED
                message = (
                    f"GREEN-AFTER: the tests are red on baseline "
                    f"{baseline_sha[:12]}, but they do NOT pass in the "
                    "current tree — the fix is missing or broken."
                )
        elif classification == "symbol-missing-red":
            verdict, code = VERDICT_BROKEN, EXIT_BROKEN
            message = (
                f"BROKEN-RUNNER: the baseline failure is only a missing-symbol "
                f"import — on {baseline_sha[:12]} the tested code does not "
                "exist yet. 0 executed tests is not a red result."
            )
            warnings.append(
                "acceptable for NEW code: the test falls because the symbol "
                "does not exist on the baseline; verify the assertions bite "
                "once the symbol exists (an empty test would fail the same "
                "way)"
            )
        else:  # no-tests / collection-error / env-error / broken
            verdict, code = VERDICT_BROKEN, EXIT_BROKEN
            detail = {
                "no-tests": "pytest collected 0 tests — nothing was executed",
                "collection-error": "collection error — the test file did not import",
                "env-error": "import error for a module outside this project "
                "(broken environment, not the code)",
                "broken": "the pytest run did not complete",
            }[classification]
            message = (
                f"BROKEN-RUNNER: {detail}. 0 collected tests is not a red "
                f"result — the check did not run (baseline {baseline_sha[:12]})."
            )
            if classification == "collection-error" and _mentions_project_symbol(
                baseline_output, project_dir
            ):
                warnings.append(
                    "acceptable for NEW code: the collection error is a "
                    "project module/symbol missing on the baseline; verify "
                    "the assertions bite once the code exists (an empty "
                    "test would fail the same way)"
                )
    finally:
        _remove_worktree(project_dir, worktree)

    return ProveRedResult(
        todo_id=todo_id,
        verdict=verdict,
        exit_code=code,
        baseline_sha=baseline_sha,
        tests=list(tests),
        copied_files=copied,
        baseline_output=baseline_output,
        current_output=current_output,
        message=message,
        warnings=warnings,
        worktree=str(worktree),
    )


# ─── Preconditions ──────────────────────────────────────────────────────


def _require_agentic(project_dir: Path) -> None:
    if not (project_dir / ".agentic").is_dir():
        raise AwfApiError(
            f"No .agentic/ found at {project_dir}. Run 'awf init' first."
        )


def _require_git_repo(project_dir: Path) -> None:
    r = run_tree(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=project_dir, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0 or r.stdout.strip() != "true":
        raise AwfApiError(f"Not a git repository: {project_dir}. Run 'git init' first.")


def _read_baseline_sha(project_dir: Path, todo_id: str) -> str:
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not sha_file.is_file():
        raise AwfApiError(
            f"baseline not found: {sha_file} — create it first with "
            f"'awf baseline {todo_id}' (or dispatch the TODO, which "
            "baselines it automatically)"
        )
    sha = sha_file.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise AwfApiError(
            f"{sha_file.name} contains {sha!r} — expected a 7-40 char git sha; "
            f"recreate it with 'awf baseline {todo_id}'"
        )
    r = run_tree(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=project_dir, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise AwfApiError(
            f"baseline sha {sha[:12]} is not a commit in this repo — "
            f"recreate it with 'awf baseline {todo_id}'"
        )
    return sha


# ─── Tests resolution ───────────────────────────────────────────────────


def _find_todo_file(project_dir: Path, todo_id: str) -> Path | None:
    candidates = (
        paths.inbox(project_dir) / f"{todo_id}.md",
        paths.done_dir(project_dir) / todo_id / "TODO.md",
    )
    for c in candidates:
        if c.is_file():
            return c
    return None


def _tests_from_contract(project_dir: Path, todo_id: str) -> list[str] | None:
    todo_file = _find_todo_file(project_dir, todo_id)
    if todo_file is None:
        return None
    try:
        content = todo_file.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        contract, _ = parse_todo_contract(content)
    except ValueError:
        return None
    if contract and contract.get("prove_red"):
        return [str(t) for t in contract["prove_red"]]
    return None


def _copy_test_files(project_dir: Path, worktree: Path, test_ids: list[str]) -> list[str]:
    """Copy files containing the given tests into the worktree.

    New (untracked) files are copied too — that is the whole point: the
    baseline checkout does not have them yet.
    """
    copied: list[str] = []
    for tid in test_ids:
        rel = tid.split("::", 1)[0].strip()
        if not rel or "\x00" in rel:
            raise AwfApiError(
                f"bad test id '{tid}' — expected a file path or file::test"
            )
        src = project_dir / rel
        if not src.is_file():
            raise AwfApiError(
                f"test file not found in the current tree: {rel} (from '{tid}')"
            )
        dst = worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if rel not in copied:
            copied.append(rel)
    return copied


# ─── Worktree lifecycle ─────────────────────────────────────────────────


def _new_worktree(project_dir: Path, sha: str, tmp_base: str | Path | None) -> Path:
    base = Path(tmp_base) if tmp_base else Path(DEFAULT_TMP_BASE)
    base.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix=f"prove-red-{sha[:8]}-", dir=base))
    worktree.rmdir()  # `git worktree add` creates the directory itself
    r = run_tree(
        ["git", "worktree", "add", "--detach", str(worktree), sha],
        cwd=project_dir, capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        shutil.rmtree(worktree, ignore_errors=True)
        raise AwfApiError(
            f"git worktree add failed: {(r.stderr or r.stdout).strip()[:400]}"
        )
    return worktree


def _remove_worktree(project_dir: Path, worktree: Path) -> None:
    try:
        run_tree(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=project_dir, capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    try:
        run_tree(
            ["git", "worktree", "prune"],
            cwd=project_dir, capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


# ─── Pytest runs ────────────────────────────────────────────────────────


_BOOTSTRAP_SOURCE = '''\
"""U4 hermeticity bootstrap — runs inside the baseline worktree.

Drops editable-install leaks before pytest starts, then hands over to
pytest.main. Must stay import-free of the project itself.
"""
import os
import sys
from pathlib import Path

worktree = Path(os.getcwd()).resolve()

# 1) PEP 660 editable installs: their meta-path finders map the project's
#    packages onto the INSTALLED tree, leaking current code into this run.
#    Finders are usually CLASSES on meta_path, so inspect the object AND
#    its metaclass for the "editable" marker.
for finder in list(sys.meta_path):
    marker = ""
    for obj in (finder, type(finder)):
        marker += (getattr(obj, "__module__", "") or "").lower()
        marker += (getattr(obj, "__name__", "") or "").lower()
    if "editable" in marker:
        sys.meta_path.remove(finder)

# 2) Old-style editable installs / other checkouts: sys.path entries that
#    also provide one of this worktree's own top-level packages.
own = set()
try:
    entries = list(worktree.iterdir())
except OSError:
    entries = []
for p in entries:
    name = p.name
    if name.startswith(".") or name in ("node_modules", "__pycache__", "venv", ".venv"):
        continue
    if p.is_dir() and (p / "__init__.py").is_file():
        own.add(name)
    elif p.is_file() and p.suffix == ".py":
        own.add(name[: -len(".py")])

if own:
    kept = []
    for entry in sys.path:
        base = Path(entry).resolve() if entry else worktree
        if base == worktree:
            kept.append(entry)
            continue
        if any(
            (base / name).is_dir() or (base / (name + ".py")).is_file()
            for name in own
        ):
            continue  # duplicate provider — this worktree is the source
        kept.append(entry)
    sys.path[:] = kept

import pytest  # noqa: E402

sys.exit(pytest.main(sys.argv[1:]))
'''


def _run_pytest(cwd: Path, test_ids: list[str], *, hermetic: bool = False) -> tuple[int, str]:
    args = [
        "-q",
        f"--timeout={PYTEST_TEST_TIMEOUT}", "-p", "no:cacheprovider",
        *test_ids,
    ]
    if hermetic:
        bootstrap = cwd / _BOOTSTRAP_NAME
        bootstrap.write_text(_BOOTSTRAP_SOURCE, encoding="utf-8")
        cmd = [sys.executable, str(bootstrap), *args]
    else:
        cmd = [sys.executable, "-m", "pytest", *args]
    try:
        proc = run_tree(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            timeout=PYTEST_RUN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 2, f"(timeout: the pytest run did not finish within {PYTEST_RUN_TIMEOUT}s)"
    out = proc.stdout or ""
    err = proc.stderr or ""
    return proc.returncode, (out + ("\n" + err if err else ""))[-8000:]


def _summary_tail(output: str) -> str:
    """The final `= N failed, M passed in Xs =` line, if any."""
    for line in reversed(output.splitlines()):
        s = line.strip()
        if s.startswith("=") and s.endswith("=") and " in " in s:
            return s
    return ""


def _project_top_levels(project_dir: Path) -> set[str]:
    """Top-level importable names that belong to THIS project."""
    levels: set[str] = set()
    try:
        entries = list(project_dir.iterdir())
    except OSError:
        return levels
    for p in entries:
        name = p.name
        if name.startswith(".") or name in ("node_modules", "__pycache__", "venv", ".venv"):
            continue
        if p.is_dir():
            levels.add(name)
        elif p.suffix == ".py":
            levels.add(name[:-3])
    return levels


_NO_MODULE_RE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_CANT_IMPORT_RE = re.compile(
    r"cannot import name ['\"]([^'\"]+)['\"]\s+from\s+['\"]([^'\"]+)['\"]"
)


def _mentions_project_symbol(output: str, project_dir: Path) -> bool:
    """True when an import error in ``output`` names a project package."""
    levels = _project_top_levels(project_dir)
    for m in _NO_MODULE_RE.finditer(output):
        top = m.group(1).split(".")[0]
        if top in levels and top not in _NON_PROJECT_TOP_LEVELS:
            return True
    for m in _CANT_IMPORT_RE.finditer(output):
        mod = m.group(2)
        top = mod.split(".")[0] if not mod.startswith(".") else ""
        if top in levels and top not in _NON_PROJECT_TOP_LEVELS:
            return True
    return False


def _classify_pytest(rc: int, output: str, project_dir: Path) -> str:
    """Classify a pytest run.

    Returns one of: ``passed``, ``real-red``, ``symbol-missing-red``,
    ``no-tests``, ``collection-error``, ``env-error``, ``broken``.

    Decision order (the first matching rule wins):
    1. rc 0 — everything passed.
    2. rc 5 — pytest collected nothing.
    3. rc 2/3/4 — interrupted or usage error: nothing was executed.
    4. rc 1 with 0 items or an error summary — collection/setup error.
    5. rc 1 with an ``AssertionError`` — a real red.
    6. rc 1 without any import/attribute marker — a genuine runtime
       error, which is a real red too (spec: "assertion OR real error").
    7. rc 1 where the failures are import/attribute errors only: either a
       project symbol is missing (expected for new code) or an external
       module is missing (broken environment).
    """
    if rc == 0:
        return "passed"
    if rc == 5:
        return "no-tests"
    if rc in (2, 3, 4):
        return "collection-error" if "error" in output.lower() else "no-tests"
    if rc != 1:
        return "broken"
    if "collected 0 items" in output:
        return "collection-error"
    if "error" in _summary_tail(output):
        return "collection-error"
    if "AssertionError" in output:
        return "real-red"
    if not re.search(r"ModuleNotFoundError|ImportError|has no attribute", output):
        return "real-red"

    levels = _project_top_levels(project_dir)
    env_hits = 0
    for m in _NO_MODULE_RE.finditer(output):
        top = m.group(1).split(".")[0]
        if top not in levels or top in _NON_PROJECT_TOP_LEVELS:
            env_hits += 1
    for m in _CANT_IMPORT_RE.finditer(output):
        mod = m.group(2)
        top = mod.split(".")[0] if not mod.startswith(".") else ""
        if top not in levels or top in _NON_PROJECT_TOP_LEVELS:
            env_hits += 1
    if env_hits:
        return "env-error"
    return "symbol-missing-red"


__all__ = [
    "ProveRedResult",
    "prove_red",
    "EXIT_RED_OK",
    "EXIT_NOT_RED",
    "EXIT_BROKEN",
    "VERDICT_RED_OK",
    "VERDICT_NOT_RED",
    "VERDICT_BROKEN",
    "VERDICT_GREEN_AFTER",
]
