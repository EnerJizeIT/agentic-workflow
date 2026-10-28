"""K1/K2/K6: specification package as data, readiness gate and volume budget.

The six-document package lives in one directory: the requirements document
plus its five companions (context, requirements review, architecture, test
specification, readiness). This module reads the headers of those documents
(K1), decides whether the package may enter development (K2) and measures the
volume budget (K6). Nothing here talks to a language model: the verdict is a
function of bytes on disk, so a repeat gives the same answer.

Public API:

- :func:`parse_spec_header` — one header (status, revision, inputs, base,
  checked revisions, package sha) with line numbers.
- :func:`parse_coverage_matrix` — the four-column coverage table (form only;
  existence of sections and tokens is checked by :func:`spec_gate`).
- :func:`fingerprint_documents` / :func:`package_fingerprint` — the K1
  ``Пакет-sha`` over the five documents (readiness excluded).
- :func:`spec_gate` — the readiness predicate, step by step in K2 order.
- :func:`count_words` / :func:`budget_rows` / :func:`check_budgets` — the K6
  volume budget.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config as awf_config

#: Author roles: statuses a document author may declare (K1). ``REVIEW`` is
#: the normal end state of an author — acceptance lives in the verdict.
AUTHOR_ROLES: tuple[str, ...] = (
    "context",
    "requirements",
    "architecture",
    "test-spec",
)
#: The verdict document (requirements review) uses its own status set.
REVIEW_ROLE = "review"
#: The readiness document (final reviewer) uses ``READY``/``BLOCKED``.
READINESS_ROLE = "readiness"
#: Fixed write order: deterministic messages, no directory-listing order.
ROLES: tuple[str, ...] = (
    "context",
    "requirements",
    "review",
    "architecture",
    "test-spec",
    "readiness",
)
#: The five documents covered by ``Пакет-sha`` (K1: readiness excluded).
FINGERPRINT_ROLES: tuple[str, ...] = (
    "context",
    "requirements",
    "review",
    "architecture",
    "test-spec",
)
ROLE_STATUSES: dict[str, frozenset[str]] = {
    "context": frozenset({"DRAFT", "REVIEW", "BLOCKED"}),
    "requirements": frozenset({"DRAFT", "REVIEW", "BLOCKED"}),
    "architecture": frozenset({"DRAFT", "REVIEW", "BLOCKED"}),
    "test-spec": frozenset({"DRAFT", "REVIEW", "BLOCKED"}),
    "review": frozenset({"PASS", "FAIL"}),
    "readiness": frozenset({"READY", "BLOCKED"}),
}
#: Filename suffixes -> role (K1: the role comes from the file name). The
#: requirements document itself has no known suffix — it is the remaining
#: top-level ``*.md`` of the package; among several unrecognized ones the file
#: named after the package directory wins, otherwise the role is ambiguous.
ROLE_SUFFIXES: tuple[tuple[str, str], ...] = (
    (".requirements-review.md", "review"),
    (".context.md", "context"),
    (".architecture.md", "architecture"),
    (".test-spec.md", "test-spec"),
    (".readiness.md", "readiness"),
)
REQUIRED_KEYS: tuple[str, ...] = ("Статус", "Ревизия", "Входы", "База кода")
VERDICT_KEYS: tuple[str, ...] = ("Проверено",)
READINESS_KEYS: tuple[str, ...] = ("Пакет-sha",)
STATUS_KEY = "Статус"
REVISION_KEY = "Ревизия"
CHECKED_KEY = "Проверено"
PACKAGE_SHA_KEY = "Пакет-sha"
BASE_KEY = "База кода"

#: K6 defaults; ``spec.budget.document`` / ``spec.budget.package`` override.
DEFAULT_DOCUMENT_BUDGET = 2200
DEFAULT_PACKAGE_BUDGET = 11000

MATRIX_TITLE = "Матрица покрытия"
MATRIX_HEADER: tuple[str, ...] = ("ID", "Источник", "Сценарий/контракт", "Проверка")

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_KEY_LINE_RE = re.compile(r"^([^\s:#][^:]*?):\s*(.*)$")
_ID_RE = re.compile(r"^(R|AC|INV)\d+$")
_INPUT_RE = re.compile(r"([A-Za-z0-9_./<>-]+)@([A-Za-z0-9_.-]+)")
_CHECKED_RE = re.compile(r"([A-Za-z0-9_./<>-]+)=([A-Za-z0-9_.-]+)")
_COMMIT_RE = re.compile(r"\b([0-9a-fA-F]{7,40})\b")
_SHA_RE = re.compile(r"\b([0-9a-f]{64})\b")
_DECLARED_ID_RE = re.compile(
    r"^\s*(?:#{1,6}\s*|Критерии\s*:\s*)?(R\d+|AC\d+|INV\d+)\b"
)
_SEPARATOR_CELL_RE = re.compile(r"^:?-{3,}:?$")


class SpecError(ValueError):
    """A package document cannot be read as K1 data.

    The message names the file and the key/row — a refusal must be fixable
    without re-reading the whole package.
    """

    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}" if path else message)


@dataclass(frozen=True)
class SpecHeader:
    """One parsed header (K1). ``lines`` maps a key to its 1-based line."""

    role: str
    path: str
    status: str
    revision: str
    inputs: dict[str, str] = field(default_factory=dict)
    code_base: str = ""
    checked: dict[str, str] = field(default_factory=dict)
    package_sha: str = ""
    lines: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class MatrixRow:
    """One coverage row: ``ID -> источник -> сценарий/контракт -> проверка``."""

    id: str
    source: str
    scenario: str
    check: str


@dataclass(frozen=True)
class SpecGate:
    """The one readiness verdict (K2)."""

    ok: bool
    reason: str
    details: tuple[str, ...] = ()
    package_dir: str = ""
    package_sha: str = ""
    actual_sha: str = ""


@dataclass(frozen=True)
class PackageLayout:
    """Discovered documents and the problems of the layout."""

    root: Path
    documents: dict[str, Path]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class BudgetRow:
    """K6 row: words of one document and its limit."""

    path: str
    words: int
    limit: int

    @property
    def over(self) -> bool:
        return self.words > self.limit


def declared_status(text: str) -> str:
    """The header's ``Статус`` value (first token) — no full K1 parse.

    The pipeline engine reads a stage verdict with this: a review report
    that says ``Статус: FAIL`` must roll the stage back to its author even
    when the rest of the strict header is incomplete. The readiness gate
    keeps :func:`parse_spec_header` (the strict reader).
    """
    for line in _header_lines(text):
        match = _KEY_LINE_RE.match(line)
        if match and match.group(1).strip() == STATUS_KEY:
            return _first_token(match.group(2))
    return ""


def _strip_value(value: str) -> str:
    """Trim backticks/spaces around a header value."""
    return value.strip().strip("`").strip()


def _first_token(value: str) -> str:
    parts = _strip_value(value).split()
    return parts[0] if parts else ""


def _header_lines(text: str) -> list[str]:
    """Lines of the header: everything before the first ``## `` heading."""
    lines = text.lstrip("\ufeff").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("## ") or line.startswith("##\t"):
            return lines[:index]
    return lines


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as e:
        raise SpecError(label, f"документ не читается как UTF-8: {e}") from None


def role_for_name(name: str) -> str | None:
    """Role of a package file by its name (K1); ``None`` for a foreign file."""
    for suffix, role in ROLE_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return role
    if name.endswith(".md") and len(name) > 3:
        return "requirements"
    return None


def parse_spec_header(text: str, role: str, path: str = "") -> SpecHeader:
    """Parse one K1 header.

    Required keys are ``Статус``, ``Ревизия``, ``Входы`` and ``База кода``;
    the verdict and readiness also require ``Проверено``, readiness —
    ``Пакет-sha``. An unknown key (``Назначение``, ``Scope``, ``Основание``)
    is metadata and does not fail the parse. A missing, empty or
    out-of-set value raises :class:`SpecError` naming the file and key.
    """
    if role not in ROLE_STATUSES:
        raise SpecError(path, f"неизвестная роль документа: {role}")

    values: dict[str, tuple[str, int]] = {}
    known = set(REQUIRED_KEYS) | set(VERDICT_KEYS) | set(READINESS_KEYS)
    for index, line in enumerate(_header_lines(text), start=1):
        match = _KEY_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        if key in values:
            if key in known:
                raise SpecError(path, f"ключ «{key}» повторяется в шапке")
            continue
        values[key] = (match.group(2), index)

    required = list(REQUIRED_KEYS)
    if role in (REVIEW_ROLE, READINESS_ROLE):
        required += list(VERDICT_KEYS)
    if role == READINESS_ROLE:
        required += list(READINESS_KEYS)
    for key in required:
        if key not in values:
            raise SpecError(path, f"нет обязательного ключа «{key}»")
        if not _strip_value(values[key][0]):
            raise SpecError(path, f"пустое значение ключа «{key}»")

    status = _first_token(values[STATUS_KEY][0])
    if status not in ROLE_STATUSES[role]:
        allowed = "/".join(sorted(ROLE_STATUSES[role]))
        raise SpecError(
            path,
            f"статус «{status}» не из набора {allowed} (ключ «{STATUS_KEY}»)",
        )

    revision = _first_token(values[REVISION_KEY][0])
    if not revision:
        raise SpecError(path, f"пустое значение ключа «{REVISION_KEY}»")

    inputs: dict[str, str] = {}
    for match in _INPUT_RE.finditer(values["Входы"][0]):
        inputs[match.group(1)] = match.group(2)

    base_match = _COMMIT_RE.search(values[BASE_KEY][0])
    if not base_match:
        raise SpecError(path, f"«{BASE_KEY}»: не найден commit sha")

    checked: dict[str, str] = {}
    if CHECKED_KEY in values:
        for match in _CHECKED_RE.finditer(values[CHECKED_KEY][0]):
            checked[match.group(1)] = match.group(2)
        if values[CHECKED_KEY][0].strip() and not checked:
            raise SpecError(
                path, f"«{CHECKED_KEY}»: нет пар файл=ревизия"
            )

    package_sha = ""
    if PACKAGE_SHA_KEY in values:
        sha_match = _SHA_RE.search(values[PACKAGE_SHA_KEY][0])
        if not sha_match:
            raise SpecError(path, f"«{PACKAGE_SHA_KEY}»: не найден hex sha256")
        package_sha = sha_match.group(1)

    return SpecHeader(
        role=role,
        path=path,
        status=status,
        revision=revision,
        inputs=inputs,
        code_base=base_match.group(1),
        checked=checked,
        package_sha=package_sha,
        lines={key: line for key, (_, line) in values.items()},
    )


def discover_documents(package_dir: str | Path) -> PackageLayout:
    """Find the six package documents and everything that does not belong.

    Files under ``assets/`` are declared attachments and never count as
    extras. Any other file (a second ``*.md``, ``README``, ``v2``, a
    subdirectory) is a finding of AC21.

    The suffix-less requirements document is the remaining top-level
    ``*.md``. When several files are unrecognized, the one named after the
    package directory is the requirements document and the others are
    extras; without such an anchor the role stays undecided and the refusal
    names every candidate instead of picking one by sort order.
    """
    root = Path(package_dir).resolve()
    if not root.is_dir():
        return PackageLayout(root, {}, problems=(f"каталог пакета не найден: {root}",))

    documents: dict[str, Path] = {}
    extras: list[str] = []
    candidates: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith("assets/"):
            continue
        if "/" in rel:
            extras.append(rel)
            continue
        role = role_for_name(path.name)
        if role == "requirements":
            candidates.append(path)
            continue
        if role is None or role in documents:
            extras.append(rel)
            continue
        documents[role] = path

    requirements_problem = ""
    if candidates:
        anchored = [path for path in candidates if path.stem == root.name]
        chosen = anchored[0] if len(anchored) == 1 else (
            candidates[0] if len(candidates) == 1 else None
        )
        if chosen is None:
            requirements_problem = (
                "роль requirements неоднозначна: "
                + ", ".join(path.name for path in candidates)
            )
        else:
            documents["requirements"] = chosen
            extras.extend(
                path.relative_to(root).as_posix()
                for path in candidates
                if path != chosen
            )

    problems = [requirements_problem] if requirements_problem else []
    problems.extend(
        f"нет документа роли «{role}»"
        for role in ROLES
        if role not in documents
        and not (role == "requirements" and requirements_problem)
    )
    problems.extend(f"лишний файл в пакете: {rel}" for rel in extras)
    return PackageLayout(root, documents, tuple(problems))


def _all_documents_text(
    layout: PackageLayout,
) -> tuple[dict[str, str], list[str]]:
    """Read every discovered document; collect (texts, problems)."""
    texts: dict[str, str] = {}
    problems: list[str] = []
    for role in ROLES:
        path = layout.documents.get(role)
        if path is None:
            continue
        rel = path.relative_to(layout.root).as_posix()
        try:
            texts[role] = _read_text(path, rel)
        except SpecError as e:
            problems.append(str(e))
    return texts, problems


def fingerprint_documents(documents: dict[str, bytes]) -> str:
    """K1 package fingerprint: ``sha256`` of ``путь\\0hex(sha256)\\n`` lines.

    Paths are sorted by their UTF-8 bytes; bytes are not normalized (CRLF and
    BOM change the result on purpose — D5).
    """
    entries = sorted(
        ((path.encode("utf-8"), raw) for path, raw in documents.items()),
        key=lambda item: item[0],
    )
    prefix = b""
    for path_bytes, raw in entries:
        digest = hashlib.sha256(raw).hexdigest().encode("ascii")
        prefix += path_bytes + b"\0" + digest + b"\n"
    return hashlib.sha256(prefix).hexdigest()


def package_fingerprint(package_dir: str | Path) -> str:
    """Fingerprint of the five documents (readiness excluded)."""
    layout = discover_documents(package_dir)
    if layout.problems:
        raise SpecError(
            str(layout.root),
            "пакет неполон: " + "; ".join(layout.problems),
        )
    documents: dict[str, bytes] = {}
    for role in FINGERPRINT_ROLES:
        path = layout.documents[role]
        documents[path.relative_to(layout.root).as_posix()] = path.read_bytes()
    return fingerprint_documents(documents)


def count_words(text: str) -> int:
    """K6 counter: ``awk '{n += NF}'`` — whitespace-separated fields per line."""
    return sum(len(line.split()) for line in text.splitlines())


def budget_limits(config: dict) -> tuple[int, int]:
    """Document and package limits from ``spec.budget.*`` (K6 defaults)."""

    def _positive(value: object, default: int) -> int:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default

    document = _positive(awf_config.get(config, "spec.budget.document"), DEFAULT_DOCUMENT_BUDGET)
    package = _positive(awf_config.get(config, "spec.budget.package"), DEFAULT_PACKAGE_BUDGET)
    return document, package


def budget_rows(
    package_dir: str | Path,
    document_limit: int = DEFAULT_DOCUMENT_BUDGET,
) -> list[BudgetRow]:
    """Words per document, in the fixed K6 order."""
    layout = discover_documents(package_dir)
    if layout.problems:
        raise SpecError(
            str(layout.root),
            "пакет неполон: " + "; ".join(layout.problems),
        )
    rows = []
    for role in ROLES:
        path = layout.documents[role]
        rel = path.relative_to(layout.root).as_posix()
        rows.append(
            BudgetRow(rel, count_words(_read_text(path, rel)), document_limit)
        )
    return rows


def check_budgets(
    package_dir: str | Path,
    *,
    document_limit: int = DEFAULT_DOCUMENT_BUDGET,
    package_limit: int = DEFAULT_PACKAGE_BUDGET,
) -> list[str]:
    """MAJOR findings of the K6 budget (empty list — the budget is fine)."""
    rows = budget_rows(package_dir, document_limit)
    findings: list[str] = []
    for row in rows:
        if row.over:
            findings.append(
                f"MAJOR: {row.path} — {row.words} слов (порог {row.limit})"
            )
    total = sum(row.words for row in rows)
    if total > package_limit:
        findings.append(
            f"MAJOR: пакет — {total} слов (порог {package_limit})"
        )
    return findings


def _headings(text: str) -> list[tuple[int, str]]:
    headings = []
    for line in text.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip()))
    return headings


def section_token_matches(title: str, token: str) -> bool:
    """``title`` names ``token``: equal, or the token as a whole word prefix.

    The ONE home of the matrix-token rule (K1/D8). ``spec_gate`` counts the
    headings a token matches; the TODO transfer asks whether a link's exact
    heading is the one a matrix row names. Two copies of this rule is how
    "which section belongs to which ID" drifts apart.
    """
    if not token:
        return False
    if title == token:
        return True
    if not title.startswith(token):
        return False
    rest = title[len(token):]
    return bool(rest) and not rest[0].isalnum() and rest[0] != "_"


def _section_count(text: str, token: str) -> int:
    """Headings that name ``token`` (see :func:`section_token_matches`)."""
    return sum(
        1 for _level, title in _headings(text) if section_token_matches(title, token)
    )


def _matrix_lines(text: str, path: str) -> list[str]:
    """Lines of the one and only coverage table, without headings."""
    headings = []
    for index, line in enumerate(text.splitlines()):
        match = _HEADING_RE.match(line)
        if match and match.group(2).strip() == MATRIX_TITLE:
            headings.append(index)
    if not headings:
        raise SpecError(path, f"нет обязательной таблицы «{MATRIX_TITLE}»")
    if len(headings) > 1:
        raise SpecError(path, f"таблица «{MATRIX_TITLE}» встречается больше одного раза")

    all_lines = text.splitlines()
    start = headings[0] + 1
    end = len(all_lines)
    for index in range(start, len(all_lines)):
        if _HEADING_RE.match(all_lines[index]):
            end = index
            break
    table = [line for line in all_lines[start:end] if line.strip().startswith("|")]
    if not table:
        raise SpecError(path, f"«{MATRIX_TITLE}»: таблица пуста")
    return table


def _split_row(line: str, path: str) -> list[str]:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    if len(cells) != len(MATRIX_HEADER):
        raise SpecError(
            path,
            f"строка матрицы должна иметь {len(MATRIX_HEADER)} колонки, "
            f"получено {len(cells)}: {line.strip()}",
        )
    return cells


def parse_coverage_matrix(text: str, path: str = "") -> list[MatrixRow]:
    """Parse the four-column coverage matrix (K1 ``AC31``) — form only.

    Structure defects (another header, three columns, a duplicate ID, an
    empty cell, a bad locator/token, both check forms in one cell) raise
    :class:`SpecError`; the existence of sections and tokens, the ID
    universe and the completeness are checked by :func:`spec_gate`.
    """
    table = _matrix_lines(text, path)
    header = _split_row(table[0], path)
    if tuple(header) != MATRIX_HEADER:
        raise SpecError(
            path,
            "заголовок матрицы должен быть "
            "| " + " | ".join(MATRIX_HEADER) + " |",
        )

    rows: list[MatrixRow] = []
    seen: set[str] = set()
    for line in table[1:]:
        cells = _split_row(line, path)
        if all(_SEPARATOR_CELL_RE.match(cell) for cell in cells):
            continue
        row_id, source, scenario, check = cells
        for name, value in zip(MATRIX_HEADER, cells):
            if not value:
                raise SpecError(path, f"пустая колонка «{name}» в строке матрицы")
        if not _ID_RE.match(row_id):
            raise SpecError(path, f"ID матрицы «{row_id}» не R#/AC#/INV#")
        if row_id in seen:
            raise SpecError(path, f"ID «{row_id}» повторяется в матрице")
        seen.add(row_id)
        _check_source(source, path, row_id)
        _check_scenario(scenario, path, row_id)
        _check_verification(check, path, row_id)
        rows.append(MatrixRow(row_id, source, scenario, check))
    return rows


def _check_source(source: str, path: str, row_id: str) -> None:
    if "#" not in source:
        raise SpecError(path, f"{row_id}: источник без «#секции»: {source}")
    rel, section = source.rsplit("#", 1)
    if not rel or not section:
        raise SpecError(path, f"{row_id}: пустой путь или секция: {source}")
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise SpecError(path, f"{row_id}: источник вне пакета: {source}")


def _check_scenario(scenario: str, path: str, row_id: str) -> None:
    for token in (part.strip() for part in scenario.split("/")):
        if not token:
            raise SpecError(path, f"{row_id}: пустой токен сценария")
        if re.fullmatch(r"[CKD]\d+", token) or token == "план":
            continue
        if token.startswith("ветка:") and token[len("ветка:"):].strip():
            continue
        raise SpecError(path, f"{row_id}: неизвестный токен сценария «{token}»")


def _check_verification(check: str, path: str, row_id: str) -> None:
    if check.startswith("метод:"):
        rationale = check[len("метод:"):].strip()
        if not rationale:
            raise SpecError(path, f"{row_id}: «метод:» без описания")
        if re.search(r"\bT\d+\b", rationale) or re.search(r"\bIMP\d+\b", rationale):
            raise SpecError(
                path,
                f"{row_id}: обе формы проверки в одной ячейке — "
                "выберите T#/IMP# или «метод:»",
            )
        return
    tokens = [part.strip() for part in check.split("/")]
    if any(not token for token in tokens):
        raise SpecError(path, f"{row_id}: пустой токен проверки")
    for token in tokens:
        if not re.fullmatch(r"(T|IMP)\d+", token):
            raise SpecError(path, f"{row_id}: неизвестный токен проверки «{token}»")
    if not any(re.fullmatch(r"T\d+", token) for token in tokens):
        raise SpecError(path, f"{row_id}: проверка без T#")


def declared_requirement_ids(text: str) -> set[str]:
    """R#/AC#/INV# declared by the requirements document.

    A declaration starts a line, optionally after a heading marker or the
    protocol's ``Критерии:`` label (the analyst template writes a criterion
    as ``Критерии: AC1 — ...``).
    """
    ids: set[str] = set()
    for line in text.splitlines():
        match = _DECLARED_ID_RE.match(line)
        if match:
            ids.add(match.group(1))
    return ids


def _token_present(text: str, token: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(token)}(?![\w])", text) is not None


def first_open_question(text: str) -> tuple[str, str] | None:
    """First row of ``## Открытые вопросы``: ``(вопрос, владелец)``.

    The table is ``ID | Вопрос | Владелец | Влияние``; a missing section or
    an empty table returns ``None`` (the caller names it separately). The
    readiness refusal names the owner, and the pipeline engine uses the
    same owner to send a BLOCKED package back to the author who owns the
    finding (requirements or architecture), not to a fixed stage.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match and match.group(2).strip() == "Открытые вопросы":
            start = index + 1
            break
    if start is None:
        return None
    for line in lines[start:]:
        if _HEADING_RE.match(line):
            break
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 4 or all(_SEPARATOR_CELL_RE.match(cell) for cell in cells):
            continue
        if cells[0] == "ID":
            continue
        if cells[1]:
            return cells[1], cells[2]
    return None


def _blocked_message(path: str, text: str) -> str:
    question = first_open_question(text)
    if question is None:
        has_section = any(
            title == "Открытые вопросы" for _level, title in _headings(text)
        )
        if has_section:
            return (
                f"{path}: статус BLOCKED, раздел «Открытые вопросы» пуст"
            )
        return f"{path}: статус BLOCKED, раздела «Открытые вопросы» нет"
    return f"{path}: BLOCKED — {question[0]} (владелец: {question[1]})"


def _step(findings: list[str]) -> tuple[bool, str, tuple[str, ...]]:
    if not findings:
        return True, "", ()
    return False, findings[0], tuple(findings[1:])


def spec_gate(project_dir: str | Path, package_dir: str | Path) -> SpecGate:
    """The readiness predicate (K2), checks in order, first failure wins.

    ``project_dir`` provides ``.agentic/config.yaml`` (budget overrides);
    ``package_dir`` is absolute or relative to the project. The returned
    reasons are deterministic: the same tree gives the same text.
    """
    project = Path(project_dir).resolve()
    raw_package = Path(package_dir)
    package = (
        raw_package.resolve()
        if raw_package.is_absolute()
        else (project / raw_package).resolve()
    )
    try:
        label = package.relative_to(project).as_posix()
    except ValueError:
        label = str(package)

    if not package.is_dir():
        return SpecGate(False, f"каталог пакета не найден: {label}", package_dir=label)

    layout = discover_documents(package)
    if layout.problems:
        _ok, reason, details = _step(list(layout.problems))
        return SpecGate(False, reason, details, package_dir=label)

    texts, read_problems = _all_documents_text(layout)
    if read_problems:
        _ok, reason, details = _step(read_problems)
        return SpecGate(False, reason, details, package_dir=label)

    headers: dict[str, SpecHeader] = {}
    parse_problems: list[str] = []
    for role in ROLES:
        rel = layout.documents[role].relative_to(layout.root).as_posix()
        try:
            headers[role] = parse_spec_header(texts[role], role, rel)
        except SpecError as e:
            parse_problems.append(str(e))
    if parse_problems:
        _ok, reason, details = _step(parse_problems)
        return SpecGate(False, reason, details, package_dir=label)

    readiness = headers[READINESS_ROLE]
    try:
        actual_sha = package_fingerprint(package)
    except SpecError as e:
        return SpecGate(
            False,
            str(e),
            (),
            package_dir=label,
            package_sha=readiness.package_sha,
        )

    def _fail(reason: str, details: tuple[str, ...] = ()) -> SpecGate:
        return SpecGate(
            False,
            reason,
            details,
            package_dir=label,
            package_sha=readiness.package_sha,
            actual_sha=actual_sha,
        )

    # 3. Author documents are not DRAFT/BLOCKED; REVIEW does not block.
    status_findings: list[str] = []
    for role in AUTHOR_ROLES:
        header = headers[role]
        if header.status == "DRAFT":
            status_findings.append(
                f"{header.path}: статус DRAFT — документ не готов"
            )
        elif header.status == "BLOCKED":
            status_findings.append(
                _blocked_message(header.path, texts[role])
            )
    if status_findings:
        _ok, reason, details = _step(status_findings)
        return _fail(reason, details)

    # 4. The requirements review is PASS.
    review = headers[REVIEW_ROLE]
    requirements = headers["requirements"]
    if review.status != "PASS":
        return _fail(f"{review.path}: вердикт {review.status} — пакет не принят")

    # 5. The verdict covers the current requirements revision.
    req_name = requirements.path
    covered = review.checked.get(req_name)
    if covered is None:
        return _fail(
            f"вердикт не покрывает {req_name} (документ на {requirements.revision})"
        )
    if covered != requirements.revision:
        return _fail(
            "устаревшая ревизия: "
            f"вердикт называет {req_name}={covered}, "
            f"документ на {requirements.revision}"
        )

    # 6. Readiness is READY and covers the six revisions.
    if readiness.status != "READY":
        return _fail(_blocked_message(readiness.path, texts[READINESS_ROLE]))
    coverage_findings: list[str] = []
    for role in ROLES:
        header = headers[role]
        name = header.path
        got = readiness.checked.get(name)
        if got is None:
            coverage_findings.append(
                f"готовность не покрывает {name} (документ на {header.revision})"
            )
        elif got != header.revision:
            coverage_findings.append(
                "устаревшая ревизия: "
                f"готовность называет {name}={got}, документ на {header.revision}"
            )
    if coverage_findings:
        _ok, reason, details = _step(coverage_findings)
        return _fail(reason, details)

    # 7. All code bases agree (prefix from 7 significant characters).
    reference_role = ROLES[0]
    reference_base = headers[reference_role].code_base[:7]
    base_findings: list[str] = []
    for role in ROLES[1:]:
        header = headers[role]
        if header.code_base[:7] != reference_base:
            base_findings.append(
                "расхождение базы: "
                f"{header.path}={header.code_base[:7]}, "
                f"{headers[reference_role].path}={reference_base}"
            )
    if base_findings:
        _ok, reason, details = _step(base_findings)
        return _fail(reason, details)

    # 8. The declared package sha equals the five-document fingerprint.
    if actual_sha != readiness.package_sha:
        return _fail(
            f"Пакет-sha не совпадает: {readiness.package_sha} → {actual_sha}"
        )

    # 9. The coverage matrix is complete (form + locators + tokens + IDs).
    try:
        rows = parse_coverage_matrix(texts[READINESS_ROLE], readiness.path)
    except SpecError as e:
        return _fail(str(e))
    matrix_findings = _matrix_findings(rows, layout, texts)
    if matrix_findings:
        _ok, reason, details = _step(matrix_findings)
        return _fail(reason, details)

    # 10. The volume budget is in norm.
    project_cfg = awf_config.load(project)
    document_limit, package_limit = budget_limits(project_cfg)
    try:
        budget_findings = check_budgets(
            package,
            document_limit=document_limit,
            package_limit=package_limit,
        )
    except SpecError as e:
        return _fail(str(e))
    if budget_findings:
        _ok, reason, details = _step(budget_findings)
        return _fail(reason, details)

    return SpecGate(
        True,
        "",
        (),
        package_dir=label,
        package_sha=readiness.package_sha,
        actual_sha=actual_sha,
    )


def _matrix_findings(
    rows: list[MatrixRow],
    layout: PackageLayout,
    texts: dict[str, str],
) -> list[str]:
    """Existence and completeness of the matrix (AC13, AC31)."""
    findings: list[str] = []
    requirements = texts["requirements"]
    architecture = texts["architecture"]
    declared = declared_requirement_ids(requirements)
    row_ids = {row.id for row in rows}

    for missing in sorted(declared - row_ids):
        findings.append(f"{missing}: нет строки в матрице покрытия")
    for extra in sorted(row_ids - declared):
        findings.append(f"{extra}: ID не объявлен в требованиях")

    for row in rows:
        rel, section = row.source.rsplit("#", 1)
        path = (layout.root / rel).resolve()
        try:
            path.relative_to(layout.root)
        except ValueError:
            findings.append(f"{row.id}: источник вне пакета: {row.source}")
            continue
        if not path.is_file():
            findings.append(f"{row.id}: файл источника не найден: {rel}")
            continue
        try:
            source_text = _read_text(path, rel)
        except SpecError as e:
            findings.append(f"{row.id}: {e}")
            continue
        count = _section_count(source_text, section)
        if count == 0:
            findings.append(f"{row.id}: секция не найдена: {row.source}")
        elif count > 1:
            findings.append(f"{row.id}: заголовок не уникален: {row.source}")
        for token in (part.strip() for part in row.scenario.split("/")):
            if re.fullmatch(r"C\d+", token) and not _token_present(requirements, token):
                findings.append(f"{row.id}: нет сценария {token}")
            elif re.fullmatch(r"[KD]\d+", token) and not _token_present(architecture, token):
                findings.append(f"{row.id}: нет контракта/решения {token}")
    return findings


__all__ = [
    "DEFAULT_DOCUMENT_BUDGET",
    "DEFAULT_PACKAGE_BUDGET",
    "MATRIX_HEADER",
    "MATRIX_TITLE",
    "ROLES",
    "ROLE_STATUSES",
    "BudgetRow",
    "MatrixRow",
    "PackageLayout",
    "SpecError",
    "SpecGate",
    "SpecHeader",
    "budget_limits",
    "budget_rows",
    "check_budgets",
    "count_words",
    "declared_requirement_ids",
    "declared_status",
    "discover_documents",
    "fingerprint_documents",
    "first_open_question",
    "package_fingerprint",
    "parse_coverage_matrix",
    "parse_spec_header",
    "role_for_name",
    "spec_gate",
]
