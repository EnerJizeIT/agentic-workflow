"""DESIGN 0238: writers inventory — rows whose probes print their own site token."""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "design" / "agentic-writers.md"
COLUMNS = ("Компонент", "Путь (поверхность → корень/лист)", "Проба", "Ожидание", "Вердикт")
MIN_ROWS, MIN_MODULES = 45, 45
PROBE_TIMEOUT_SEC, SELECTOR_TIMEOUT_SEC = 120, 300

OPERATIONS_RE = re.compile(r"atomic_write_text|write_text|write_bytes|\.unlink\("
                           r"|remove\(|rmtree|shutil\.move")
VERDICT_RE = re.compile(r"^(?:barrier|exception\+[^|]+|unit-\d{4})$")
# The eight kinds declared in the document; any other spelling is a typo that
# would silently switch the anti-vacuum checks off.
EXCEPTION_KINDS = frozenset({
    "outside-surface", "generic-primitive", "init-bootstrap", "platform-gate",
    "housekeeping", "policy-source", "false-positive", "test-tooling",
})
SELECTOR_RE = re.compile(r"^(tests/[\w./-]+\.py)::(?:[A-Z]\w*::)?(test_[\w]+)$")
SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")

# The primitive selector is citable only by a module that calls the primitive.
PRIMITIVE_WRITE_SELECTOR = ("tests/negative/test_roadmap_r1_safety.py::"
                            "TestOtherManagedWriters::test_managed_write_text_refuses_symlinked_agentic")
# State-store refusal tests prove ``write_operation`` only.
STATE_STORE_SELECTOR_PREFIX = ("tests/negative/test_miniwave_m3_managedfs_round2.py"
                               "::test_write_operation_")

ALLOWED_COMMANDS = {"grep", "test", "ls"}  # git/python3 can write
FORBIDDEN_IN_PROBE = (">", "<", "|", "&", ";", "$(", "`", "\n", "\r")

AGENTIC_DEFAULT_RE = re.compile(r"^\s*[A-Z][A-Z0-9_]*\s*=\s*[^#]*\.agentic(?:[/\"'])")
AGENTIC_LITERAL_RE = re.compile(r"""["']\.agentic(?:[/"'])""")
REQUIRED_KEYS = ("plan_checkpoint", "RUN-EVIDENCE", "include_untracked", ".claim",
                 ".carry_over", "REJECT-", "GATES-", "handoff", "PIPELINE-",
                 "DOCTRINE-ASSEMBLED", "BASELINE-", "SALVAGE-", "RUN-REPORT-",
                 "SERVICE-RUN-REPORT-", "mkdir", "service", "hygiene", "dispatch",
                 "require_posix", "design.md")

_SELECTOR_CACHE: dict[str, tuple[str | None, str]] = {}
_PROBE_CACHE: dict[str, ProbeResult] = {}


class DocError(ValueError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None


@dataclass(frozen=True)
class Row:
    component: str
    path: str
    probe: str
    expectation: str
    verdict: str
    line_no: int


def _component_module(component: str) -> str:
    match = re.match(r"([\w./-]+\.py)\b", component)
    return match.group(1) if match else ""


def _site_tokens(component: str) -> set[str]:
    """Tokens naming the row's own site: the module stem and its code spans."""
    tokens = set(re.findall(r"`([A-Za-z_]\w*)`", component))
    module = _component_module(component)
    if module:
        tokens.add(Path(module).stem)
    return {token for token in tokens if token}


def _cells(line: str) -> list[str] | None:
    stripped = line.strip()
    cells = [cell.strip().strip("`").strip() for cell in stripped.strip("|").split("|")]
    return cells if stripped.startswith("|") else None


def _inventory_block(text: str) -> list[str]:
    for key, group in groupby(text.splitlines(), key=lambda line: line.strip().startswith("|")):
        block = list(group) if key else []
        header = _cells(block[0]) if block else None
        if header and tuple(header) == COLUMNS:
            return block
    raise DocError("no inventory table with columns " + " | ".join(COLUMNS))


def _check_cell_wrapping(line: str, line_no: int) -> None:
    """The verdict cell either has no code span or closes the one it opens."""
    raw = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if len(raw) != len(COLUMNS):
        return
    verdict = raw[-1]
    if "`" in verdict and not (verdict.startswith("`") and verdict.endswith("`")):
        raise DocError(f"line {line_no}: the verdict cell opens a code span "
                       "it never closes")
    if "`" in verdict.strip("`"):
        raise DocError(f"line {line_no}: the verdict cell nests a code span "
                       "inside its own — it renders split, and the inner "
                       "backticks hide the verdict text")


def _parse_table(text: str) -> list[Row]:
    block = _inventory_block(text)
    offset = text.splitlines().index(block[0]) + 1
    rows: list[Row] = []
    for index, line in enumerate(block[1:], start=2):
        cells = _cells(line)
        if cells is None or all(SEPARATOR_RE.fullmatch(c) for c in cells):
            continue
        _check_cell_wrapping(line, offset + index - 1)
        if len(cells) != len(COLUMNS):
            raise DocError(f"line {index}: {len(cells)} columns, expected "
                           f"{len(COLUMNS)}")
        for column, cell in zip(COLUMNS, cells):
            if not cell:
                raise DocError(f"line {index}: empty {column!r} cell")
        rows.append(Row(*cells, line_no=offset + index - 1))
    return rows


def _selector_test_name(selector: str) -> str:
    return selector.rsplit("::", 1)[-1]


def _check_barrier(row: Row) -> None:
    match = SELECTOR_RE.fullmatch(row.probe)
    if not match:
        raise DocError(f"line {row.line_no}: a barrier row needs a pytest selector 'tests/...py::test_name'")
    if not (REPO_ROOT / match.group(1)).is_file():
        raise DocError(f"line {row.line_no}: selector file does not exist: "
                       f"{row.probe}")
    module = _component_module(row.component)
    if row.probe == PRIMITIVE_WRITE_SELECTOR:
        source = REPO_ROOT / module
        if not module or not source.is_file():
            raise DocError(f"line {row.line_no}: {module!r} is not a module")
        if "managed_write_text(" not in source.read_text(encoding="utf-8",
                                                         errors="replace"):
            raise DocError(f"line {row.line_no}: {module} does not call "
                           "managed_write_text(...) — the primitive selector "
                           "proves a different surface")
    if row.probe.startswith(STATE_STORE_SELECTOR_PREFIX) and not module.startswith(
            "awf/infra/state_store/"):
        raise DocError(f"line {row.line_no}: the state-store refusal test is "
                       f"cited for {module or row.component!r} — it proves "
                       "state_store.write_operation, not this surface")
    expected = row.expectation.strip()
    if expected != _selector_test_name(row.probe):
        raise DocError(f"line {row.line_no}: a barrier row's expectation is the test name "
                       f"{_selector_test_name(row.probe)!r}, not {expected!r}")
    tokens = _site_tokens(row.component)
    if row.probe == PRIMITIVE_WRITE_SELECTOR:
        tokens.add("managed_write_text")  # the row's site IS the primitive
    if row.probe.startswith(STATE_STORE_SELECTOR_PREFIX):
        tokens.add("write_operation")  # the row's site IS the state-store writer
    test_text = (REPO_ROOT / match.group(1)).read_text(encoding="utf-8",
                                                       errors="replace")
    if not tokens or not any(token in test_text for token in tokens):
        raise DocError(f"line {row.line_no}: the cited test file "
                       f"{match.group(1)} never mentions the row's site "
                       f"{sorted(tokens)} — a passing refusal test for another "
                       "surface proves nothing here")


def _exception_kind(verdict: str) -> str:
    return verdict.split("+", 1)[1].split(":", 1)[0].strip()


def _check_row(row: Row) -> None:
    probe, verdict = row.probe, row.verdict.strip()
    if not probe.strip():
        raise DocError(f"line {row.line_no}: empty probe")
    if not VERDICT_RE.fullmatch(verdict):
        raise DocError(f"line {row.line_no}: verdict {verdict!r} is not barrier | exception+<reason> | unit-NNNN")
    if verdict.startswith("exception+"):
        kind = _exception_kind(verdict)
        if kind not in EXCEPTION_KINDS:
            raise DocError(f"line {row.line_no}: unknown exception kind {kind!r} — "
                           f"declared kinds are {sorted(EXCEPTION_KINDS)}")
    if verdict == "barrier":
        _check_barrier(row)
        return
    for token in FORBIDDEN_IN_PROBE:
        if token in probe:
            raise DocError(f"line {row.line_no}: probe contains forbidden "
                           f"{token!r} (read-only commands only)")
    try:
        argv = shlex.split(probe)
    except ValueError as exc:
        raise DocError(f"line {row.line_no}: probe does not parse: {exc}") from exc
    if not argv or argv[0] not in ALLOWED_COMMANDS:
        raise DocError(f"line {row.line_no}: command {argv[0] if argv else ''!r} is not "
                       f"in the read-only allowlist {sorted(ALLOWED_COMMANDS)}")
    if not verdict.startswith("unit-"):
        return
    if argv[0] != "grep" or not any(a.endswith(".py") for a in argv):
        raise DocError(f"line {row.line_no}: a unit row needs a grep of the "
                       "live site (file.py + pattern)")
    probe_files = [a for a in argv if a.endswith(".py")]
    module = _component_module(row.component)
    if module and probe_files != [module]:
        raise DocError(f"line {row.line_no}: probe greps {probe_files}, not {module} — proves its own site")


def _check_tokens(rows: list[Row]) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        module = _component_module(row.component) or row.component
        token = row.expectation.strip()
        if not token:
            raise DocError(f"line {row.line_no}: empty expectation — the probe "
                           "output must be checked against a site token")
        key = f"{module}\0{token}"
        if key in seen:
            raise DocError(f"line {row.line_no}: expectation {token!r} repeats "
                           f"line {seen[key]} inside {module} — it cannot tell "
                           "the sites apart")
        seen[key] = str(row.line_no)


def validate_document(text: str) -> list[Row]:
    rows = _parse_table(text)
    if not rows:
        raise DocError("inventory table has no data rows")
    for row in rows:
        _check_row(row)
    _check_tokens(rows)
    return rows


def tree_modules(root: Path = REPO_ROOT) -> list[str]:
    found: list[str] = []
    for path in sorted((root / "awf").rglob("*.py")):
        if OPERATIONS_RE.search(path.read_text(encoding="utf-8", errors="replace")):
            found.append(path.relative_to(root).as_posix())
    return found


def _run_selector(selector: str) -> tuple[str | None, str]:
    if selector in _SELECTOR_CACHE:
        return _SELECTOR_CACHE[selector]
    stdout = ""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", selector, "-v", "--timeout=120", "-p",
             "no:cacheprovider"], cwd=REPO_ROOT, capture_output=True, text=True,
            timeout=SELECTOR_TIMEOUT_SEC, check=False)
        stdout = proc.stdout + proc.stderr
        problem = None if proc.returncode == 0 else f"selector failed: {stdout.strip()[-300:]}"
    except subprocess.TimeoutExpired:
        problem = f"selector timed out after {SELECTOR_TIMEOUT_SEC}s"
    except OSError as exc:
        problem = f"selector did not run: {exc}"
    _SELECTOR_CACHE[selector] = (problem, stdout)
    return problem, stdout


def _execute_probe(probe: str) -> ProbeResult:
    """Run a read-only probe once; every consumer reads this cache."""
    cached = _PROBE_CACHE.get(probe)
    if cached is not None:
        return cached
    try:
        argv = shlex.split(probe)
        proc = subprocess.run(argv, cwd=REPO_ROOT,
                              capture_output=True, text=True,
                              timeout=PROBE_TIMEOUT_SEC, check=False)
        result = ProbeResult(proc.returncode, proc.stdout, proc.stderr, None)
    except subprocess.TimeoutExpired:
        result = ProbeResult(None, "", "",
                             f"probe timed out after {PROBE_TIMEOUT_SEC}s")
    except OSError as exc:
        result = ProbeResult(None, "", "", f"probe did not run: {exc}")
    _PROBE_CACHE[probe] = result
    return result


def run_probe(row: Row) -> str | None:
    expected = row.expectation.strip()
    if row.verdict.strip() == "barrier":
        problem, stdout = _run_selector(row.probe)
        if problem is not None:
            return problem
        passed = re.compile(rf"{re.escape(expected)}\b.*\bPASSED\b")
        if not any(passed.search(line) for line in stdout.splitlines()):
            return (f"selector output has no '{expected} ... PASSED' line — "
                    "a skipped barrier proves nothing")
        return None
    result = _execute_probe(row.probe)
    if result.error is not None:
        return result.error
    if result.returncode != 0:
        return (f"exit {result.returncode}, expected 0 "
                f"(stderr: {result.stderr.strip()[:200]})")
    if expected not in result.stdout:
        return (f"probe output does not contain {expected!r}: "
                f"{result.stdout.strip()[:200]!r}")
    return None


def _probe_stdout(row: Row) -> str | None:
    """Probe output, or None when the probe did not run cleanly."""
    if row.verdict.strip() == "barrier":
        return None
    result = _execute_probe(row.probe)
    return result.stdout if result.returncode == 0 else None


def _probe_line_numbers(row: Row) -> set[int]:
    """Lines of the module this row's grep probe actually matched."""
    stdout = _probe_stdout(row)
    if not stdout:
        return set()
    return {int(match.group(1)) for line in stdout.splitlines()
            if (match := re.match(r"(\d+):", line))}


FS_OPERATION_RE = re.compile(
    r"atomic_write_text|write_text|write_bytes|managed_write_api_text|\.touch\(|\.mkdir\(|"
    r"\.unlink\(|os\.unlink\(|os\.link\(|shutil\.move\(|\.rename\(|\.replace\(|"
    r"rmtree\(|rmdir\(|\bopen\s*\(")
NOTE_ROW_MARKERS = ("note «BLOCKED cleared»",)


def _matched_source_lines(row: Row) -> list[str]:
    """Source lines of the module that the row's grep probe actually matched."""
    module = _component_module(row.component)
    path = REPO_ROOT / module
    if not module or not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [lines[number - 1] for number in sorted(_probe_line_numbers(row))
            if 0 < number <= len(lines)]


def operation_less_rows(rows: list[Row]) -> list[Row]:
    """Unit rows that claim a filesystem site but never probe its operation.

    A path-construction line next to the writer proves the path exists, not
    the write: deleting ``atomic_write_text(report, ...)`` would keep such a
    row green. The A3 note row is about note text, not a filesystem call.
    """
    return [row for row in rows
            if row.verdict.strip() == "unit-0239"
            and not any(marker in row.component for marker in NOTE_ROW_MARKERS)
            and not any(FS_OPERATION_RE.search(line)
                        for line in _matched_source_lines(row))]


def _surface(path: str) -> str:
    """The surface a path cell names — the part before its parenthesised note."""
    head = path.strip().split(" (", 1)[0]
    return head.strip().strip("`").strip()


def _is_agentic_surface(path: str) -> bool:
    return _surface(path).startswith(".agentic")


def _agentic_writes(module: str, root: Path = REPO_ROOT) -> list[tuple[int, str]]:
    """Lines that name a `.agentic` path on a write or default site."""
    path = root / module
    if not module or not path.is_file():
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    return [(number, line) for number, line in enumerate(text.splitlines(), 1)
            if AGENTIC_DEFAULT_RE.search(line)
            or (OPERATIONS_RE.search(line) and AGENTIC_LITERAL_RE.search(line))]


def outside_surface_problems(rows: list[Row], root: Path = REPO_ROOT) -> list[str]:
    """An outside-surface row is red when the module writes into `.agentic`
    directly and no row of that module documents the state surface."""
    covered = {_component_module(row.component) for row in rows if _is_agentic_surface(row.path)}
    problems: list[str] = []
    for row in rows:
        if not row.verdict.strip().startswith("exception+outside-surface"):
            continue
        module = _component_module(row.component)
        hits = _agentic_writes(module, root)
        if hits and module not in covered:
            problems.append(
                f"line {row.line_no}: {module} writes under .agentic "
                f"(line {hits[0][0]}) — it is not an outside-surface exception")
    return problems


def multi_match_problems(rows: list[Row]) -> list[str]:
    """A probe matching several lines is red unless its expectation occurs on
    exactly one output line — otherwise deleting a sibling site stays green."""
    problems: list[str] = []
    for row in rows:
        stdout = _probe_stdout(row)
        if not stdout:
            continue
        matched = [line for line in stdout.splitlines() if re.match(r"\d+:", line)]
        if len(matched) <= 1:
            continue
        expected = row.expectation.strip()
        occurrences = [line for line in stdout.splitlines() if expected in line]
        if len(occurrences) != 1:
            problems.append(
                f"line {row.line_no}: probe matches {len(matched)} lines and "
                f"{expected!r} occurs on {len(occurrences)} of them — it cannot "
                "tell its own site from the neighbour")
    return problems


def _touch_sites(root: Path = REPO_ROOT) -> dict[str, set[int]]:
    sites: dict[str, set[int]] = {}
    for path in sorted((root / "awf").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = {i for i, line in enumerate(text.splitlines(), 1) if ".touch(" in line}
        if lines:
            sites[path.relative_to(root).as_posix()] = lines
    return sites


BARE_OPEN_WRITE_RE = re.compile(
    r"""(?<![\w.`'"])open\s*\([^)]*["'][^"']*(?:[wax]|\+)[^"']*["']""")


def bare_open_write_sites(root: Path = REPO_ROOT) -> dict[str, set[int]]:
    """Bare ``open(..., "w"/"a"/"x"/"+")`` sites — the F2 "голое создание" class.

    Managed openers (``open_managed``/``open_append_log``/``os.fdopen``) and
    ``Path.open`` stay out: the primitive, not this scan, holds their path.
    """
    sites: dict[str, set[int]] = {}
    for path in sorted((root / "awf").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = {i for i, line in enumerate(text.splitlines(), 1)
                 if not line.lstrip().startswith("#")
                 and BARE_OPEN_WRITE_RE.search(line)}
        if lines:
            sites[path.relative_to(root).as_posix()] = lines
    return sites


def lock_creation_sites(root: Path = REPO_ROOT) -> dict[str, set[int]]:
    """F1: the win32 branch of ``_lock.py`` creates the lock with bare calls."""
    path = root / "awf" / "_lock.py"
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = {i for i, line in enumerate(text.splitlines(), 1)
             if ".mkdir(" in line or BARE_OPEN_WRITE_RE.search(line)}
    return {"awf/_lock.py": lines} if lines else {}


def uncovered_sites(sites: dict[str, set[int]], rows: list[Row]) -> dict[str, list[int]]:
    """Sites (module -> lines) no row's grep probe ever matched."""
    covered: dict[str, set[int]] = {}
    for row in rows:
        covered.setdefault(_component_module(row.component), set()).update(
            _probe_line_numbers(row))
    return {module: sorted(lines - covered.get(module, set()))
            for module, lines in sites.items() if lines - covered.get(module, set())}


def shared_site_pairs(rows: list[Row]) -> list[str]:
    """Two rows of one module whose probes match the same line: neither can
    tell its own site from the neighbour's (deleting one site stays green)."""
    by_module: dict[str, list[tuple[str, set[int]]]] = {}
    for row in rows:
        lines = _probe_line_numbers(row)
        if lines:
            by_module.setdefault(_component_module(row.component),
                                 []).append((f"line {row.line_no}", lines))
    problems: list[str] = []
    for module, items in by_module.items():
        for index, (label_a, lines_a) in enumerate(items):
            for label_b, lines_b in items[index + 1:]:
                overlap = lines_a & lines_b
                if overlap and lines_a != lines_b:
                    problems.append(f"{module}: {label_a} and {label_b} both "
                                    f"match line(s) {sorted(overlap)}")
    return problems


CLASS_PATTERNS = {
    "write": re.compile(r"atomic_write_text|(?<!managed_)write_text|write_bytes"),
    "touch": re.compile(r"\.touch\("),
    "mkdir": re.compile(r"\.mkdir\("),
    "unlink": re.compile(r"\.unlink\(|os\.unlink\("),
    "move": re.compile(r"shutil\.move\("),
    "rename": re.compile(r"\.rename\(|\.replace\(dst\)"),
    "rmtree": re.compile(r"rmtree\("),
}
CLASS_WORDS = {
    "write": re.compile(r"atomic_write_text|write_text|write_bytes|писател", re.I),
    "touch": re.compile(r"touch", re.I),
    "mkdir": re.compile(r"mkdir", re.I),
    "unlink": re.compile(r"unlink|удал", re.I),
    "move": re.compile(r"shutil\.move|move|перенос", re.I),
    "rename": re.compile(r"rename|replace", re.I),
    "rmtree": re.compile(r"rmtree", re.I),
}


def class_sites(root: Path = REPO_ROOT) -> dict[str, dict[str, set[int]]]:
    """Operation classes of each module, counted from the tree.

    Comment-only mentions are skipped: they name no filesystem call. The
    ``write`` class excludes ``managed_write_text`` — the primitive holds
    that path and the barrier rows, not this inventory, prove it.
    """
    found: dict[str, dict[str, set[int]]] = {}
    for path in sorted((root / "awf").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        module = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for name, pattern in CLASS_PATTERNS.items():
                if pattern.search(line):
                    found.setdefault(module, {}).setdefault(name, set()).add(number)
    return found


def row_classes(row: Row) -> set[str]:
    """Classes a row documents: a class keyword in its site text or probe.

    A barrier selector prints no line numbers, so its component names the
    class (``mkdir``, ``unlink``); a grep row names it in the probe or in
    the expectation it prints. Either way the class must exist in the tree
    — ``uncovered_classes`` checks that separately.
    """
    text = " ".join((row.component, row.probe, row.expectation))
    return {name for name, word in CLASS_WORDS.items() if word.search(text)}


def uncovered_classes(rows: list[Row],
                      root: Path = REPO_ROOT) -> list[tuple[str, str]]:
    """(module, class) pairs present in the tree with no row naming them."""
    covered: dict[str, set[str]] = {}
    for row in rows:
        covered.setdefault(_component_module(row.component), set()).update(
            row_classes(row))
    return [(module, name) for module, classes in class_sites(root).items()
            for name in classes if name not in covered.get(module, set())]


def check_document(text: str) -> tuple[list[Row], list[str]]:
    rows = validate_document(text)
    failures = [f"line {row.line_no} ({row.component}): {problem}"
                for row in rows if (problem := run_probe(row)) is not None]
    failures.extend(f"{problem} (outside-surface)" for problem in outside_surface_problems(rows))
    failures.extend(f"{problem} (multi-match)" for problem in multi_match_problems(rows))
    return rows, failures


def check_module_coverage(text: str, modules: list[str] | None = None) -> list[str]:
    rows = validate_document(text)
    modules = tree_modules() if modules is None else modules
    components = [_component_module(row.component) for row in rows]
    return [module for module in modules if module not in components]


def test_inventory_document_is_green() -> None:
    assert DOC.is_file(), f"missing design document: {DOC}"
    text = DOC.read_text(encoding="utf-8")
    rows, failures = check_document(text)
    assert not failures, "probes diverged from the tree:\n" + "\n".join(failures)
    assert len(rows) >= MIN_ROWS, f"{len(rows)} rows, expected >= {MIN_ROWS}"
    modules = tree_modules()
    assert len(modules) >= MIN_MODULES, f"module scan: {len(modules)}"
    missing = check_module_coverage(text, modules)
    assert not missing, ("modules with writers but no row:\n" + "\n".join(missing))
    table_text = "\n".join(_inventory_block(text))
    for key in REQUIRED_KEYS:
        assert key in table_text, f"no inventory row covers {key!r}"


def test_inventory_runs_are_idempotent() -> None:
    text = DOC.read_text(encoding="utf-8")
    _PROBE_CACHE.clear()
    _SELECTOR_CACHE.clear()
    first_rows, first_failures = check_document(text)
    _PROBE_CACHE.clear()
    _SELECTOR_CACHE.clear()
    second_rows, second_failures = check_document(text)
    assert first_failures == second_failures == []
    assert first_rows == second_rows
    third_rows, third_failures = check_document(text)
    assert third_failures == []
    assert third_rows == second_rows
    probes = {row.probe for row in third_rows if row.verdict.strip() != "barrier"}
    assert set(_PROBE_CACHE) == probes, (
        "the cached run must reuse every probe result, not re-execute some "
        "silently")


def test_every_touch_site_is_itemized() -> None:
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    covered: dict[str, set[int]] = {}
    for row in rows:
        covered.setdefault(_component_module(row.component), set()).update(
            _probe_line_numbers(row))
    uncovered = {module: sorted(lines - covered.get(module, set()))
                 for module, lines in _touch_sites().items()
                 if lines - covered.get(module, set())}
    assert "awf/_lock.py" in {_component_module(r.component) for r in rows}
    assert not uncovered, f".touch sites without their own row: {uncovered}"


def test_bare_open_write_sites_are_itemized() -> None:
    """F2: a bare ``open(..., "w"/"a"/"x"/"+")`` site needs its own row."""
    sites = bare_open_write_sites()
    assert "awf/_lock.py" in sites, (
        "the bare open in the _lock.py win32 branch is not seen by the scan")
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    uncovered = uncovered_sites(sites, rows)
    assert not uncovered, f"bare open write sites without their own row: {uncovered}"


def _assert_lock_rows(rows: list[Row]) -> None:
    """The win32 branch is reachable, so it is a unit row, not an exception.

    ``run_state._ensure_lock_file`` refuses before ``locked()`` (the managed
    opener gates POSIX), but ``api/hygiene.py`` and
    ``infra/state_store/_lock.py::project_lock`` call ``locked()`` directly:
    the win32 branch creates ``.agentic/state/.lock`` with bare ``mkdir`` +
    ``open`` before any POSIX refusal. A row that calls that "dead" hides a
    live fail-closed violation.
    """
    lock_rows = [row for row in rows
                 if _component_module(row.component) == "awf/_lock.py"]
    assert len(lock_rows) >= 2, (
        "the win32 mkdir and lock-file rows must stay in the inventory; "
        "the branch is live; unit-0239 translates it or refuses, not silently here")
    not_unit = [row for row in lock_rows
                if row.verdict.strip() != "unit-0239"]
    assert not not_unit, (
        "the win32 branch is reachable through direct locked() callers "
        "(api/hygiene.py, infra/state_store/_lock.py) — a policy violation "
        f"awaiting unit-0239, not an exception: {[row.verdict for row in not_unit]}")


def test_lock_win32_creation_sites_are_itemized() -> None:
    """F1: the ``_lock.py`` win32 ``mkdir`` and lock file are separate sites
    and both rows stay pinned to ``unit-0239`` (live; translate or refuse in 0239)."""
    sites = lock_creation_sites()
    assert len(sites.get("awf/_lock.py", ())) >= 2, (
        "the scan must see both the win32 mkdir and the lock file open")
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    uncovered = uncovered_sites(sites, rows)
    assert not uncovered, f"_lock.py creation sites without their own row: {uncovered}"
    _assert_lock_rows(rows)


def test_lock_row_losing_its_unit_verdict_is_red() -> None:
    """A row that relabels the reachable branch ``exception+dead`` is red."""
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    mutated = [replace(row, verdict="exception+dead (fail-closed)")
               if _component_module(row.component) == "awf/_lock.py"
               else row for row in rows]
    with pytest.raises(AssertionError):
        _assert_lock_rows(mutated)



def test_unit_rows_do_not_share_a_site() -> None:
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    problems = shared_site_pairs(rows)
    assert not problems, "rows that cannot tell their site apart:\n" + "\n".join(problems)


def test_unit_rows_probe_their_operation_line() -> None:
    """T3: a writer row proves the write call, not the path line beside it."""
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    offenders = [f"line {row.line_no}: {row.component} matches "
                 f"{_matched_source_lines(row)[:1]} — not an operation line"
                 for row in operation_less_rows(rows)]
    assert not offenders, ("unit rows must probe their own operation call:\n"
                           + "\n".join(offenders))


def test_writer_row_probing_the_path_line_is_red() -> None:
    """The mutation T3 describes: put a writer row's probe back on the path."""
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    target = next(row for row in rows
                  if row.component.startswith("awf/api/run.py")
                  and row.verdict.strip() == "unit-0239"
                  and "service evidence" in row.component)
    mutated = [replace(target, probe='grep -n \'f"RUN-EVIDENCE-{todo_id}.md"\' awf/api/run.py',
                       expectation='f"RUN-EVIDENCE-{todo_id}.md"')
               if row is target else row for row in rows]
    assert operation_less_rows(mutated), (
        "a path-line probe must not pass for a writer row")


def test_every_module_class_is_itemized() -> None:
    """Per-module class completeness: a class present in the tree needs a row."""
    rows = validate_document(DOC.read_text(encoding="utf-8"))
    missing = uncovered_classes(rows)
    assert not missing, "module classes without a row:\n" + "\n".join(
        f"{module}: {name}" for module, name in missing)


def test_new_class_in_a_covered_module_is_red(tmp_path: Path) -> None:
    """A new operation class in an already-covered module is not silent."""
    module = tmp_path / "awf" / "a.py"
    module.parent.mkdir(parents=True)
    module.write_text('x.write_text("a")\n', encoding="utf-8")
    row = _row("awf/a.py", 'grep -n "write_text" awf/a.py')
    rows = validate_document(_doc(row))
    assert uncovered_classes(rows, root=tmp_path) == []
    module.write_text('x.write_text("a")\n(tmp).mkdir(parents=True)\n',
                      encoding="utf-8")
    assert ("awf/a.py", "mkdir") in uncovered_classes(rows, root=tmp_path)


def test_class_scanner_ignores_comments_and_managed_writers(tmp_path: Path) -> None:
    module = tmp_path / "awf" / "a.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        '# x.unlink() in prose only\n'
        'managed_write_text(p, "x")\n'
        'x.unlink()\n', encoding="utf-8")
    assert class_sites(tmp_path) == {"awf/a.py": {"unlink": {3}}}


HEADER = "| " + " | ".join(COLUMNS) + " |\n|---|---|---|---|---|\n"


def _doc(*rows: str) -> str:
    return "# test\n\n" + HEADER + "\n".join(rows) + "\n"


BROKEN_ROWS = {
    "6 columns, expected 5": "| c | p | `grep -n x awf/a.py` | `x` | `unit-0239` | extra |",
    "empty 'Проба' cell": "| c | p |  | `x` | `unit-0239` |",
    "empty 'Ожидание' cell": "| c | p | `grep -n x awf/a.py` |  | `unit-0239` |",
    "verdict": "| c | p | `grep -n x awf/a.py` | `x` | `fine` |",
}
FORBIDDEN_PROBES = [
    ("grep -n x awf/a.py > out", "forbidden"),
    ("ls & touch /tmp/opencode/qa-0238-bypass", "forbidden"),
    ("grep -n x awf/a.py; touch x", "forbidden"),
    ("rm -rf awf", "allowlist"),
    ("git checkout -- .", "allowlist"),
    ("git ls-files", "allowlist"),
    ("python3 -c 'open(\"x\",\"w\")'", "allowlist"),
]
STRUCTURAL_REJECTIONS = [
    ("| c | p | `grep -n managed_write_text awf/x.py` | `pass` | `barrier` |",
     "barrier row needs a pytest selector"),
    ("| c | p | `tests/negative/__missing__.py::test_x` | `test_x` | `barrier` |",
     "selector file does not exist"),
    ("| c | p | `test -e awf/x.py` | `awf/x.py` | `unit-0239` |", "unit row needs a grep"),
    ("| c | p | `tests/negative/test_miniwave_m3d_unlinks.py"
     "::test_agent_stage_refuses_symlinked_outbox` | `pass` | `barrier` |",
     "expectation is the test name"),
]


def _token(probe: str) -> str:
    if probe.startswith("tests/"):
        return probe.rsplit("::", 1)[-1]
    match = re.match(r"""^grep\s+-n\s+(["'])(.+)\1\s""", probe)
    return match.group(2) if match else "x"


def _row(component: str, probe: str, verdict: str = "unit-0239",
         expect: str | None = None) -> str:
    token = _token(probe) if expect is None else expect
    return f"| {component} · x | p | `{probe}` | `{token}` | `{verdict}` |"


class TestTableContract:
    def test_prose_without_a_table_is_red(self) -> None:
        with pytest.raises(DocError, match="no inventory table"):
            validate_document("# just prose\nno table here\n")

    @pytest.mark.parametrize("match", list(BROKEN_ROWS))
    def test_malformed_tables_are_red(self, match: str) -> None:
        with pytest.raises(DocError, match=match):
            validate_document(_doc(BROKEN_ROWS[match]))

    @pytest.mark.parametrize("probe, match", FORBIDDEN_PROBES)
    def test_forbidden_probe_commands_are_red(self, probe: str, match: str) -> None:
        with pytest.raises(DocError, match=match):
            validate_document(_doc(f"| c | p | `{probe}` | `x` | `unit-0239` |"))

    @pytest.mark.parametrize("row, match", STRUCTURAL_REJECTIONS)
    def test_structural_rejections_are_red(self, row: str, match: str) -> None:
        with pytest.raises(DocError, match=match):
            validate_document(_doc(row))

    def test_nonexistent_probe_is_red(self) -> None:
        rows = validate_document(
            _doc("| c | p | `grep -n x awf/__definitely_missing__.py` | `x` | `unit-0239` |"))
        assert any(run_probe(row) for row in rows)

    def test_module_coverage_catches_a_missing_row(self) -> None:
        text = _doc(_row("awf/a.py", "grep -n x awf/a.py"))
        assert check_module_coverage(text, ["awf/a.py", "awf/b.py"]) == ["awf/b.py"]

    def test_coverage_ignores_a_module_named_only_in_a_probe(self) -> None:
        text = _doc(_row("awf/a.py", "grep -n awf/b awf/a.py"))
        assert check_module_coverage(text, ["awf/a.py", "awf/b.py"]) == ["awf/b.py"]

    def test_unit_probe_must_grep_its_own_module(self) -> None:
        with pytest.raises(DocError, match="proves its own site"):
            validate_document(_doc(_row("awf/a.py", "grep -n x awf/b.py")))

    def test_expectation_must_appear_in_the_probe_output(self) -> None:
        rows = validate_document(_doc(_row(
            "awf/_proc.py", 'grep -n "def " awf/_proc.py', expect="a_different_site")))
        assert run_probe(rows[0])

    def test_neighbour_token_is_red(self) -> None:
        from dataclasses import replace
        rows = validate_document(_doc(
            _row("awf/_proc.py", 'grep -n "kill_process_tree" awf/_proc.py',
                 expect="kill_process_tree"),
            _row("awf/_proc.py", 'grep -n "def ppid_of" awf/_proc.py', expect="def ppid_of")))
        assert run_probe(replace(rows[0], expectation=rows[1].expectation))

    def test_shared_site_rows_are_red(self) -> None:
        rows = validate_document(_doc(
            _row("awf/_proc.py", 'grep -n "kill_process_tree" awf/_proc.py'),
            _row("awf/_proc.py", 'grep -n "def " awf/_proc.py', expect="def ppid_of")))
        assert shared_site_pairs(rows)

    def test_duplicate_expectations_inside_a_module_are_red(self) -> None:
        with pytest.raises(DocError, match="repeats"):
            validate_document(_doc(
                _row("awf/a.py", "grep -n SAME awf/a.py", expect="SAME"),
                _row("awf/a.py", "grep -n SAME awf/a.py", expect="SAME")))

    def test_class_qualified_selector_is_accepted(self) -> None:
        rows = validate_document(
            _doc(_row("awf/api/dashboard.py", PRIMITIVE_WRITE_SELECTOR, "barrier")))
        assert len(rows) == 1

    def test_primitive_selector_needs_a_managed_write_site(self) -> None:
        with pytest.raises(DocError, match="does not call managed_write_text"):
            validate_document(_doc(_row("awf/_atomic.py", PRIMITIVE_WRITE_SELECTOR, "barrier")))

    def test_state_store_selector_is_not_reused_for_another_surface(self) -> None:
        probe = STATE_STORE_SELECTOR_PREFIX + "refuses_symlinked_state"
        with pytest.raises(DocError, match="state_store.write_operation"):
            validate_document(_doc(_row("awf/api/dashboard.py", probe, "barrier")))

    def test_unknown_exception_kind_is_red(self) -> None:
        with pytest.raises(DocError, match="unknown exception kind"):
            validate_document(_doc(
                _row("awf/metrics.py", 'grep -n "write_text" awf/metrics.py',
                     "exception+outside_surface")))

    def test_exception_kind_typo_cannot_disable_the_anti_vacuum_check(self) -> None:
        text = DOC.read_text(encoding="utf-8")
        mutated = text.replace("exception+outside-surface", "exception+outside_surface")
        assert mutated != text, "the document lost its outside-surface rows"
        with pytest.raises(DocError, match="unknown exception kind"):
            validate_document(mutated)

    def test_barrier_selector_from_an_unrelated_file_is_red(self) -> None:
        probe = ("tests/negative/test_miniwave_m3c_writers.py::"
                 "test_run_evidence_write_refuses_symlinked_context")
        with pytest.raises(DocError, match="never mentions the row's site"):
            validate_document(_doc(_row("awf/todos.py", probe, "barrier")))


METRICS_CACHE_ROW = (
    "| awf/metrics.py · кэш | `.agentic/state/metrics_models_cache.json` | "
    '`grep -n "DEFAULT_MODELS_CACHE =" awf/metrics.py` | `metrics_models_cache` | '
    "`unit-0239` |")
METRICS_DESKTOP_ROW = (
    "| awf/metrics.py · отчёт | `~/Desktop` (вне проекта) | "
    "`grep -n \"out_dir = str(desktop)\" awf/metrics.py` | "
    "`out_dir = str(desktop)` | "
    "`exception+outside-surface: отчёт на рабочем столе` |")


class TestSiteDiscrimination:
    def test_outside_surface_with_an_agentic_default_is_red(self) -> None:
        rows = validate_document(_doc(METRICS_DESKTOP_ROW))
        assert outside_surface_problems(rows), (
            "metrics defaults its caches under .agentic/state — an outside-surface "
            "row must not hide that")

    def test_outside_surface_is_green_when_agentic_rows_cover_the_module(self) -> None:
        rows = validate_document(_doc(METRICS_DESKTOP_ROW, METRICS_CACHE_ROW))
        assert outside_surface_problems(rows) == []

    def test_multi_match_probe_without_site_token_is_red(self) -> None:
        rows = validate_document(_doc(_row(
            "awf/api/dispatch.py",
            'grep -n "md_path.unlink(missing_ok=True)" awf/api/dispatch.py',
            expect="md_path.unlink(missing_ok=True)")))
        assert multi_match_problems(rows), (
            "two unlink sites share the probe and the expectation — deleting one "
            "would stay green")

    def test_multi_match_probe_with_a_site_token_is_green(self) -> None:
        rows = validate_document(_doc(_row(
            "awf/api/dispatch.py",
            'grep -n -A4 "^            md_path.unlink(missing_ok=True)" awf/api/dispatch.py',
            expect="outbox has a DONE closure")))
        assert multi_match_problems(rows) == []

    def test_swapping_a_site_token_for_a_neighbour_is_red(self) -> None:
        from dataclasses import replace
        rows = validate_document(_doc(
            _row("awf/api/dispatch.py",
                 'grep -n -A4 "^            md_path.unlink(missing_ok=True)" awf/api/dispatch.py',
                 expect="outbox has a DONE closure"),
            _row("awf/api/dispatch.py",
                 'grep -n -B1 "^            md_path.unlink(missing_ok=True)" awf/api/dispatch.py',
                 expect="if baseline fails (prevents orphan")))
        mutated = [replace(rows[0], expectation=rows[1].expectation), rows[1]]
        assert multi_match_problems(mutated)


    def test_outside_surface_is_red_even_when_the_path_prose_names_agentic(self) -> None:
        row = METRICS_DESKTOP_ROW.replace("`~/Desktop` (вне проекта)",
                                          "`~/Desktop` (вне `.agentic`)")
        assert outside_surface_problems(validate_document(_doc(row))), (
            "prose in the path cell that merely mentions .agentic must not "
            "cover a module whose defaults write under .agentic")

    def test_inline_agentic_write_is_red(self, tmp_path: Path) -> None:
        module = tmp_path / "awf" / "fake.py"
        module.parent.mkdir(parents=True)
        module.write_text('(root / ".agentic" / "state" / "x").write_text("x")\n',
                          encoding="utf-8")
        row = ("| awf/fake.py · x | `~/Desktop` (вне проекта) | "
               "`grep -n x awf/fake.py` | `x` | "
               "`exception+outside-surface: тестовый модуль` |")
        rows = validate_document(_doc(row))
        assert outside_surface_problems(rows, root=tmp_path), (
            "a write op with a .agentic literal on its line needs its own row")

    def test_bare_open_scanner_ignores_reads_comments_and_managed_openers(
            self, tmp_path: Path) -> None:
        module = tmp_path / "awf" / "fake.py"
        module.parent.mkdir(parents=True)
        module.write_text(
            'a = open(p, "rb")\n'
            'with open(p, "w") as f:\n'
            '    f.write("x")\n'
            '# open(p, "a")\n'
            'fd = os.fdopen(fd, "w")\n'
            'b = p.open("w")\n', encoding="utf-8")
        assert bare_open_write_sites(tmp_path) == {"awf/fake.py": {2}}, (
            "only the bare open-for-write call is a site")

    def test_dropping_the_lock_open_row_is_red(self) -> None:
        rows = validate_document(DOC.read_text(encoding="utf-8"))
        without = [row for row in rows if "open(lock_file" not in row.probe]
        assert "awf/_lock.py" in uncovered_sites(bare_open_write_sites(), without)

    def test_dropping_the_lock_mkdir_row_is_red(self) -> None:
        rows = validate_document(DOC.read_text(encoding="utf-8"))
        without = [row for row in rows
                   if "state_dir.mkdir(parents=True" not in row.probe]
        assert "awf/_lock.py" in uncovered_sites(lock_creation_sites(), without)

    def test_skipped_selector_does_not_prove_a_barrier(self, monkeypatch) -> None:
        selector = PRIMITIVE_WRITE_SELECTOR
        name = _selector_test_name(selector)
        row = Row("awf/api/dashboard.py · x", "p", selector, name, "barrier", 0)
        monkeypatch.setattr(sys.modules[__name__], "_run_selector",
                            lambda _selector: (None, f"{selector} SKIPPED [100%]"))
        assert run_probe(row), "a skipped refusal test must not count as its barrier"

    def test_verdict_cell_must_close_its_code_span(self) -> None:
        with pytest.raises(DocError, match="never closes"):
            validate_document(
                _doc("| c | p | `grep -n x awf/a.py` | `x` | `unit-0239 |"))

    def test_nested_code_span_in_the_verdict_cell_is_red(self) -> None:
        with pytest.raises(DocError, match="nests a code span"):
            validate_document(_doc(
                "| c | p | `grep -n x awf/a.py` | `x` | "
                "`exception+outside-surface: явный `out=` — выбор` |"))


def test_design_md_points_to_the_writers_inventory() -> None:
    text = (REPO_ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    lines = [line for line in text.splitlines()
             if line.startswith("- **Запись состояния — fail-closed:**")]
    assert len(lines) == 1, "the fail-closed policy line is missing or duplicated"
    assert "agentic-writers.md" in lines[0], (
        "the policy line must name the inventory instead of promising every writer")
    assert "все писатели файлов под" not in lines[0], (
        "the old blanket promise survived — the inventory is the single source")
