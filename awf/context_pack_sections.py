"""Section bodies of the stage context pack (R2/K2).

Split from :mod:`awf.context_pack` by meaning: this module only turns the
existing artifacts (TODO, git, handoff, GATES, stage-log, role, doctrine)
into the lines of a section. The pack module owns the order, the caps
budget and the file. Nothing here reads the environment or the clock — the
same artifacts must give the same lines (AC6, AC8).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from . import doctrine, paths
from ._markdown import fence_flags, lines_below, sections_with_prefix
from .infra.git_model import GitModelError, GitRepository, display_path
from .pipeline import Stage
from .todos import review_round

#: The domain heading the pack carries verbatim in section 5 (R2/AC5).
REVIEW_PREFIX = "REVIEW-фикс"

#: Line caps for the sections R2 allows to shrink (K2). Sections 2, 5 and 8
#: are never cut — the contract, the review bodies and the sha list are the
#: reason the pack exists.
SECTION_CAPS = {"1": 10, "3": 20, "4": 20, "6": 10, "7": 12, "9": 15}

#: AC7: the sum of the capped sections must stay within this budget.
TOTAL_CAP = 150

#: Body tail of the stage log shown in section 5 (K2).
LOG_TAIL = 20

_CONTRACT_SECTIONS = ("Что сделать", "Границы", "Приёмка", "Чек-лист завершения")
_REF_RE = re.compile(r"`([^`\n]+)`")
_REF_SUFFIXES = (
    ".py", ".md", ".ts", ".tsx", ".js", ".json", ".yaml", ".yml",
    ".sh", ".toml", ".cfg", ".ini", ".sql", ".go", ".rs",
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def na(reason: str) -> str:
    """AC9: an absent input says so — and why."""
    return f"n/a: нет данных ({reason})"


def clip(lines: list[str], cap: int, link: str) -> list[str]:
    """Keep at most ``cap`` lines; the surplus becomes one pointer line."""
    if len(lines) <= cap:
        return lines
    return lines[: cap - 1] + [f"- … сокращено: {link}"]


def rel_path(project_dir: Path, path: Path) -> str:
    try:
        return str(path.relative_to(project_dir))
    except ValueError:
        return str(path)


def handoff_path(project_dir: Path, todo_id: str, stage: Stage | None) -> Path | None:
    """The previous stage's handoff: stage name first, its role as legacy.

    ``resolve_prev_handoffs`` (the spawner) reads the same pair: a unit that
    started before the stage/role rename has only the role-named file, and
    the pack must show the handoff the stage will actually receive.
    """
    if stage is None:
        return None
    for name in (stage.name, stage.role):
        if not name:
            continue
        candidate = paths.handoff_dir(project_dir) / f"{name}-{todo_id}.md"
        if candidate.is_file():
            return candidate
    return None


def baseline_sha(project_dir: Path, todo_id: str) -> str:
    """The unit's baseline sha, "" when the file is absent."""
    path = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="surrogateescape").strip()


def stage_log(project_dir: Path, todo_id: str, role: str) -> Path | None:
    """The stage log ``awf-<role>-<todo>.out`` (the writer's own name)."""
    if not role:
        return None
    candidate = paths.logs_dir(project_dir) / f"awf-{role}-{todo_id}.out"
    return candidate if candidate.is_file() else None


def _contract_headings(text: str) -> list[tuple[int, str, str]]:
    """``(line index, heading line, block name)`` for each contract block.

    ONE predicate for "this TODO carries the contract": the check that
    decides the ``n/a`` line and the collector that renders the bodies
    walk the same headings. Two spellings of the match let a heading with
    a suffix pass the check and render an empty body (TODO-0367 review).
    Fenced example headings are opaque — a ``## Что сделать`` quoted in a
    code block is not the contract (TODO-0367 QA).
    """
    lines = text.split("\n")
    flags = fence_flags(lines)
    out: list[tuple[int, str, str]] = []
    for name in _CONTRACT_SECTIONS:
        for index, line in enumerate(lines):
            if flags[index]:
                continue
            if line.startswith("## ") and line[3:].strip().startswith(name):
                out.append((index, line, name))
                break
    return out


def contract_section(text: str, link: str) -> tuple[list[str], bool]:
    """Section 2: the TODO header and the contract blocks, verbatim.

    Returns the lines and whether the contract was found at all — the
    caller's ``n/a`` line names the missing blocks from the same table.
    The body is the shared walker's (``lines_below``): a heading with a
    suffix is the same block to the check and to the body.
    """
    found = _contract_headings(text)
    if not found:
        names = ", ".join(f"«{name}»" for name in _CONTRACT_SECTIONS)
        return [na(f"{link} без контракта: нет блоков {names}")], False
    lines = text.split("\n")
    flags = fence_flags(lines)
    head_end = next(
        (i for i, ln in enumerate(lines) if not flags[i] and ln.startswith("## ")),
        len(lines),
    )
    out = list(lines[:head_end])
    while out and not out[-1].strip():
        out.pop()
    for index, heading, _name in found:
        body = "\n".join(lines_below(text, index, 2)).strip()
        if out:
            out.append("")
        out.append(heading)
        if body:
            out.extend(body.split("\n"))
    return out, True


def refs(text: str) -> list[str]:
    """Section 6: paths named by the TODO plus ``source_ref``, first found."""
    found: list[str] = []
    for match in _REF_RE.finditer(text):
        token = match.group(1).strip()
        if "/" in token or token.endswith(_REF_SUFFIXES):
            if token not in found and "\n" not in token:
                found.append(token)
    ref = re.search(r"^source_ref:\s*(.+)$", text, re.MULTILINE)
    if ref:
        path = ref.group(1).strip().strip("'\"")
        if path and path not in found:
            found.insert(0, path)
    return found


def failed_gates(project_dir: Path, todo_id: str) -> list[str]:
    """Failed sections of ``GATES-<todo>.md`` — the red evidence of section 5."""
    path = paths.context_dir(project_dir) / f"GATES-{todo_id}.md"
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return []
    out: list[str] = []
    chunk: list[str] = []
    for line in text.split("\n"):
        if line.startswith("## ") and chunk:
            _keep_failed(chunk, out)
            chunk = []
        chunk.append(line)
    _keep_failed(chunk, out)
    return out


def _keep_failed(chunk: list[str], out: list[str]) -> None:
    if any(re.search(r"\bFAIL\b", line) for line in chunk):
        out.extend(chunk)
        out.append("")


def role_file(role: str, project_dir: Path) -> Path | None:
    """Role file via the ONE resolver; a missing role is an ``n/a`` input."""
    try:
        from .supervisor import resolve_role_file

        return resolve_role_file(role, project_dir)
    except (RuntimeError, OSError):
        return None


def digests(
    project_dir: Path, todo_id: str, stage: Stage | None, handoff: Path | None
) -> list[tuple[str, str, str]]:
    """Section 8 inputs: ``(label, shown path, sha256|absent)`` (K2)."""
    rows: list[tuple[str, str, str]] = []
    todo_file = paths.todo_path(project_dir, todo_id)
    rows.append(("todo", rel_path(project_dir, todo_file) if todo_file else "-",
                 sha(todo_file.read_text(encoding="utf-8", errors="surrogateescape"))
                 if todo_file else "absent"))
    baseline = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    rows.append(("baseline", rel_path(project_dir, baseline),
                 sha(baseline.read_text(encoding="utf-8", errors="surrogateescape"))
                 if baseline.is_file() else "absent"))
    role_path = role_file(stage.role, project_dir) if stage is not None and stage.role else None
    rows.append(("role", rel_path(project_dir, role_path) if role_path else "-",
                 sha(role_path.read_text(encoding="utf-8", errors="surrogateescape"))
                 if role_path else "absent"))
    text = doctrine.assemble_doctrine(project_dir)
    rows.append(("doctrine", ".agentic/doctrine", sha(text) if text else "absent"))
    rows.append(("handoff", rel_path(project_dir, handoff) if handoff else "-",
                 sha(handoff.read_text(encoding="utf-8", errors="surrogateescape"))
                 if handoff else "absent"))
    return rows


def review_sections(text: str) -> list[tuple[str, str]]:
    """``## REVIEW-фикс`` sections with their bodies, in file order (R2/AC5)."""
    return sections_with_prefix(text, REVIEW_PREFIX)


def previous_stage(stages: list[Stage], stage: str) -> Stage | None:
    for i, item in enumerate(stages):
        if item.name == stage or item.id == stage:
            return stages[i - 1] if i > 0 else None
    return None


def delta(
    project_dir: Path,
    baseline: str | None,
    gates: list[str],
    reviews: list[tuple[str, str]],
) -> list[str]:
    """Section 9 (K9): what changed since the previous attempt.

    The round comes from the SAME fence-aware headings section 5 shows, not
    from a second reader of the raw TODO: a fenced ``## REVIEW-фикс``
    example must not raise the round the delta names while section 5 counts
    one real section (TODO-0367 QA).
    """
    headings = [heading for heading, _body in reviews]
    rounds = [n for n in (review_round(h) for h in headings) if n is not None]
    round_no = max(rounds) if rounds else None
    count = len(headings)
    changed: list[str] = []
    if baseline:
        # Paths relative to the baseline are ONE owner's fact:
        # ``GitRepository.changed_paths`` (docs/design/single-owners.md) —
        # never a printed ``git diff --name-only`` read.
        try:
            repo = GitRepository(project_dir)
            changed = [display_path(p) for p in repo.changed_paths(baseline)]
        except GitModelError:
            changed = []
    if not changed and not count:
        return ["- нет изменений"]
    out = [f"- виток: {round_no if round_no is not None else count}"]
    if changed:
        shown = ", ".join(changed[:10])
        more = f" (+{len(changed) - 10} ещё)" if len(changed) > 10 else ""
        out.append(f"- изменённые файлы: {shown}{more}")
    else:
        out.append("- изменённые файлы: нет")
    if headings:
        out.append(f"- новая секция: {headings[-1]}; тело — в секции 5")
    red = [line.strip() for line in gates if re.search(r"\bFAIL\b", line)][:3]
    out.append(f"- красные тесты: {'; '.join(red) if red else 'нет'}")
    out.append("- прошлая сессия не является источником фактов; проверяй дерево")
    return out
