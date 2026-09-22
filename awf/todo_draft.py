"""U11 (E3): ``awf todo-draft`` — task-file skeleton from AUDIT-INDEX.md.

The generator transfers FACTS from the audit registry (finding id/sev/
type/title, its unit, file references inside titles) into a dispatch-
valid task-file skeleton and leaves the INTENT to the supervisor: the
front-matter keys stay commented-out placeholders, the criteria and
scope sections carry explicit «супервизор дополняет» markers. No
invented criteria, no network — a local index file only.

Index format (the 2026-09 audit registry):
- findings table ``| ID | Sev | Тип | Заголовок | Итерация | Фикс | Статус |``
- units table ``| Юнит | Тема | ID (суммарно) | Фикс | Волна |``
Escaped pipes (``\\|``) inside cells are supported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_INDEX_NAME = "AUDIT-INDEX.md"

#: Path-like tokens (``awf/run_state.py:55``, ``docs/x.md``) — the file
#: scope is extracted from registry titles, not invented.
_FILE_RE = re.compile(
    r"\b[\w.-]+(?:/[\w.-]+)*\.(?:py|md|sh|bash|yaml|yml|json|txt|toml|"
    r"cfg|conf|ini|html|css|js|ts)\b",
    re.IGNORECASE,
)

_FRONT_MATTER = """\
---
# Супервизор объявляет проверки (docs/unit-contract.md) — снять «#» со строк:
# verify: ["python3 -m pytest tests/unit/test_x.py -q"]
# gates: ["contracts", "ratchet"]
---
"""

_SAFETY = """\
## Safety

- Safety-инварианты проекта (AGENTS.md, .agentic/doctrine/) — прочитать до правок.
- НЕ `git commit` и `git stash` на пачку: коммитит awf-гейт после approve супервизора; правки оставить в рабочем дереве.
- Сомнение в скоупе или критерии — один точный вопрос (BLOCKED), не догадки.
"""


class TodoDraftError(Exception):
    """User-facing error: unknown query, missing index, existing --out."""


@dataclass
class AuditIndex:
    findings: list[dict[str, str]] = field(default_factory=list)
    units: list[dict[str, str]] = field(default_factory=list)


def _split_row(line: str) -> list[str]:
    """Split a markdown table row on UNESCAPED pipes, unescape ``\\|``."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = re.split(r"(?<!\\)\|", inner)
    return [c.replace("\\|", "|").strip() for c in cells]


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c != "") and any(cells)


def parse_audit_index(text: str) -> AuditIndex:
    """Extract the findings and units tables from an AUDIT-INDEX.md text."""
    index = AuditIndex()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|"):
            cells = _split_row(line)
            first = cells[0] if cells else ""
            if first == "ID" and len(cells) >= 6:
                j = i + 1
                while j < len(lines) and lines[j].strip().startswith("|"):
                    row = _split_row(lines[j].strip())
                    if row and row[0] and not _is_separator_row(row):
                        index.findings.append(
                            {
                                "id": row[0],
                                "sev": row[1] if len(row) > 1 else "",
                                "type": row[2] if len(row) > 2 else "",
                                "title": row[3] if len(row) > 3 else "",
                                "iteration": row[4] if len(row) > 4 else "",
                                "fix": row[5] if len(row) > 5 else "",
                                "status": row[6] if len(row) > 6 else "",
                            }
                        )
                    j += 1
                i = j
                continue
            if first == "Юнит" and len(cells) >= 3:
                j = i + 1
                while j < len(lines) and lines[j].strip().startswith("|"):
                    row = _split_row(lines[j].strip())
                    if row and row[0] and not _is_separator_row(row):
                        index.units.append(
                            {
                                "unit": row[0],
                                "theme": row[1] if len(row) > 1 else "",
                                "id_count": row[2] if len(row) > 2 else "",
                                "fix": row[3] if len(row) > 3 else "",
                                "wave": row[4] if len(row) > 4 else "",
                            }
                        )
                    j += 1
                i = j
                continue
        i += 1
    return index


def extract_files(text: str) -> list[str]:
    """Path-like tokens from a registry title (line numbers stripped),
    deduplicated, first occurrence order."""
    seen: list[str] = []
    for m in _FILE_RE.finditer(text):
        token = m.group(0)
        if token not in seen:
            seen.append(token)
    return seen


def _norm_finding_id(query: str) -> str:
    """``AUD-02-03`` / ``aud02-03`` → ``AUD02-03``; others pass through."""
    s = query.upper().replace("-", "").replace(" ", "")
    m = re.fullmatch(r"([A-Z]+)(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(1)}{m.group(2)}-{m.group(3)}"
    return query.upper()


def _unit_in_text(unit_id: str, text: str) -> bool:
    """``FU-13`` reference without prefix-collisions (FU-1 ≠ FU-13)."""
    return re.search(
        rf"(?:^|[^A-Za-z0-9]){re.escape(unit_id)}(?![0-9])", text
    ) is not None


def _unit_row(index: AuditIndex, unit_id: str) -> dict[str, str] | None:
    return next((u for u in index.units if u["unit"] == unit_id), None)


def _unit_of_finding(f: dict[str, str]) -> str:
    """The FU-NN a finding belongs to. In the registry the reference sits
    in the status cell («✅ FU-13 (abc1234)»); fix is checked as fallback."""
    for cell in (f.get("status", ""), f.get("fix", "")):
        m = re.search(r"FU-\d+", cell)
        if m:
            return m.group(0)
    return ""


def _findings_of_unit(index: AuditIndex, unit_id: str) -> list[dict[str, str]]:
    return [
        f
        for f in index.findings
        if _unit_in_text(unit_id, f.get("status", "") + " " + f.get("fix", ""))
    ]


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _findings_table(findings: list[dict[str, str]]) -> str:
    lines = ["| ID | Sev | Тип | Суть |", "|----|-----|-----|------|"]
    for f in findings:
        lines.append(
            f"| {f['id']} | {f['sev']} | {f['type']} | {_escape_cell(f['title'])} |"
        )
    return "\n".join(lines)


def _files_section(files: list[str]) -> str:
    if files:
        body = "\n".join(f"- `{f}`" for f in files)
    else:
        body = "- (файлы в заголовках реестра не указаны — супервизор определяет)"
    return (
        "## Скоуп файлов\n\n"
        f"{body}\n\n"
        "<!-- СДЕЛАТЬ: супервизор сужает скоуп — что трогать, что НЕ трогать -->\n\n"
    )


def _skeleton(
    title: str,
    context_lines: list[str],
    findings: list[dict[str, str]],
    files: list[str],
    index_name: str,
) -> str:
    context = "\n".join(context_lines)
    return (
        f"<!-- auto-draft · awf todo-draft · source: {index_name} · "
        "супервизор дополняет замысел -->\n"
        f"{_FRONT_MATTER}\n"
        f"# {title}\n\n"
        f"**Контекст.** {context}\n\n"
        "<!-- СДЕЛАТЬ: супервизор дополняет замысел — зачем берём именно сейчас, "
        "что сделать, как принимать -->\n\n"
        "## Находки (факты из реестра)\n\n"
        f"{_findings_table(findings)}\n\n"
        f"{_files_section(files)}"
        "## Критерии готовности\n\n"
        "<!-- СДЕЛАТЬ: супервизор пишет критерии; генератор их не выдумывает -->\n\n"
        "## Verify\n\n"
        "<!-- СДЕЛАТЬ: супервизор заполняет front-matter (verify/gates) и "
        "перечисляет команды -->\n\n"
        f"{_SAFETY}"
    )


def _finding_draft(f: dict[str, str], index: AuditIndex, index_name: str) -> str:
    unit_id = _unit_of_finding(f)
    unit = _unit_row(index, unit_id) if unit_id else None
    context = [
        f"находка {f['id']} (Sev {f['sev']}, {f['type']}), итерация "
        f"{f['iteration']}; статус: {f['status'] or '—'}."
    ]
    if unit:
        context.append(
            f"Юнит {unit['unit']} — {unit['theme']} (размер {unit['fix']}, "
            f"волна {unit['wave']})."
        )
    elif unit_id:
        context.append(f"Юнит {unit_id} (строка юнита в сводке не найдена).")
    context.append(f"Источник: {index_name}.")
    title = f"TODO-???? — {f['id']}: {f['title'].replace(chr(10), ' ')}"
    return _skeleton(title, context, [f], extract_files(f["title"]), index_name)


def _unit_draft(unit_id: str, index: AuditIndex, index_name: str) -> str:
    unit = _unit_row(index, unit_id)
    members = _findings_of_unit(index, unit_id)
    if not unit and not members:
        known = ", ".join(u["unit"] for u in index.units) or "—"
        raise TodoDraftError(
            f"юнит {unit_id} не найден в реестре. Известные юниты: {known}"
        )
    theme = unit["theme"] if unit else "(тема не найдена в сводке)"
    context = []
    if unit:
        context.append(
            f"фикс-юнит {unit_id} (размер {unit['fix']}, волна {unit['wave']}), "
            f"находок в реестре: {len(members)}."
        )
    else:
        context.append(f"фикс-юнит {unit_id}, находок в реестре: {len(members)}.")
    context.append(f"Источник: {index_name}.")
    title = f"TODO-???? — {unit_id}: {theme.replace(chr(10), ' ')}"
    files: list[str] = []
    for f in members:
        for p in extract_files(f["title"]):
            if p not in files:
                files.append(p)
    return _skeleton(title, context, members, files, index_name)


def build_todo_draft(query: str, index: AuditIndex, index_name: str) -> str:
    """Render the task-file skeleton for a finding (``AUD02-03``) or a unit
    (``FU-13``). Unknown queries raise TodoDraftError with known ids."""
    q = query.strip()
    if re.fullmatch(r"FU-\d+", q.upper()):
        return _unit_draft(q.upper(), index, index_name)
    fid = _norm_finding_id(q)
    for f in index.findings:
        if f["id"].upper() == fid:
            return _finding_draft(f, index, index_name)
    known = ", ".join(f["id"] for f in index.findings[:12])
    more = f" (+{len(index.findings) - 12})" if len(index.findings) > 12 else ""
    units = ", ".join(u["unit"] for u in index.units) or "—"
    raise TodoDraftError(
        f"finding {query} not found in the registry. "
        f"Known findings: {known or '—'}{more}; units: {units}"
    )


def resolve_index_path(
    explicit: str | None, project_dir: Path
) -> Path:
    """--index wins; then <project>/AUDIT-INDEX.md, then the project's
    .agentic/context/."""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise TodoDraftError(f"--index: no such file {p}")
    candidates = (
        project_dir / _INDEX_NAME,
        project_dir / ".agentic" / "context" / _INDEX_NAME,
    )
    for c in candidates:
        if c.is_file():
            return c
    raise TodoDraftError(
        f"{_INDEX_NAME} not found — pass --index PATH. Searched: "
        + ", ".join(str(c) for c in candidates)
    )


def make_todo_draft(
    query: str,
    *,
    index_path: str | None = None,
    project_dir: str | Path = ".",
    out: str | Path | None = None,
    force: bool = False,
) -> tuple[Path | None, str]:
    """Full CLI-level flow: resolve index, parse, render, write/return.

    Returns ``(out_path, draft_text)``; ``out_path`` is None in stdout
    mode. Refuses to overwrite an existing ``--out`` without ``force``.
    """
    pd = Path(project_dir)
    idx_path = resolve_index_path(index_path, pd)
    index = parse_audit_index(idx_path.read_text(encoding="utf-8"))
    draft = build_todo_draft(query, index, str(idx_path))
    if out:
        out_path = Path(out)
        if out_path.exists() and not force:
            raise TodoDraftError(
                f"{out} already exists — pass --force to overwrite"
            )
        if not out_path.parent.exists():
            out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(draft, encoding="utf-8")
        return out_path, draft
    return None, draft


__all__: list[Any] = [
    "AuditIndex",
    "TodoDraftError",
    "build_todo_draft",
    "extract_files",
    "make_todo_draft",
    "parse_audit_index",
    "resolve_index_path",
]
