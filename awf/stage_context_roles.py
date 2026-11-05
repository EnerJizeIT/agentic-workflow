"""K6: the role core — one file, a selection by ``##`` markers (R4, arch §7).

The role file stays the single source: the text is not copied into
``*.core.md`` and a second copy never exists. Sections are taken in priority
order until the ``ROLE`` limit; the rest becomes a table of contents
``- §<heading> — <path>``, so the stage still knows what it did not read.

Coverage is measured from the CORE (everything the markers cover, header
included): ``floor(taken × 100 / total)``, below 70 is ``CTX-ROLE-LOW``
(AC38). A 69,5 % core reads as 69 %.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .bounded_prompt import cut_marker, dropped_lines

#: arch §7: marker keywords per priority; a heading outside this table is
#: not core — it only appears in the table of contents.
ROLE_MARKERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (1, ("Instructions", "Инструкции", "Quick Reference", "Роль")),
    (
        2,
        (
            "Выход",
            "Handoff contract",
            "Отчёт",
            "Готовность",
            "Формат находок",
            "Итог",
            "Результат",
        ),
    ),
    (3, ("Режим", "Режимы", "Вход")),
    (4, ("Запрет", "Prohibitions", "Проверка", "Находки")),
    (5, (
        "AWF-OVERRIDE", "Политика тестов", "Дисциплина доставки инвариантов",
        "Iteration adaptation", "Бюджет файла",
    )),
)

#: The header before the first ``##`` never exceeds this many characters.
HEADER_LIMIT = 500

#: AC38: below this share of the core the stage gets ``CTX-ROLE-LOW``.
ROLE_LOW_PCT = 70

#: A section smaller than this leftover is not worth a truncated head.
MIN_PARTIAL = 200

_HEADING = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)

#: A role file opens with YAML front matter (``name``/``description``/...).
#: It is metadata, not role content: without stripping it the "header" was
#: the first 500 characters of YAML and a cut ``ROLE`` block delivered a
#: fragment of the description instead of the instructions.
_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)


def _strip_front_matter(text: str) -> str:
    """*text* without a leading ``---`` YAML block; anything else untouched."""
    return _FRONT_MATTER.sub("", text, count=1)


@dataclass
class RoleSelection:
    """What one role file gave to the stage text."""

    text: str = ""
    taken: int = 0
    total: int = 0
    pct: int = 100
    dropped: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def low(self) -> bool:
        """AC38: the coverage is under :data:`ROLE_LOW_PCT`."""
        return self.pct < ROLE_LOW_PCT


def marker_priority(heading: str) -> int | None:
    """The arch §7 priority of *heading*; ``None`` when it is not core."""
    lowered = heading.lower()
    for priority, words in ROLE_MARKERS:
        if any(word.lower() in lowered for word in words):
            return priority
    return None


def _split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_HEADING.finditer(text))
    if not matches:
        return text, []
    header = text[: matches[0].start()]
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append((match.group("title"), text[match.start() : end]))
    return header, sections


def select_role_core(
    text: str,
    family: str,
    limit: int,
    *,
    source: str = "",
) -> RoleSelection:
    """K6: the stage's part of a role file, its coverage and its omissions.

    The ``checklist`` row has no ``ROLE`` block at all (C3): it returns an
    empty selection instead of a second rule at the call site.
    """
    if not text or family == "reviewer/checklist" or limit <= 0:
        return RoleSelection(source=source)

    text = _strip_front_matter(text)
    header, sections = _split_sections(text)
    header = header[:HEADER_LIMIT]
    ranked: list[tuple[int, int, str, str]] = []
    toc: list[str] = []
    for index, (title, body) in enumerate(sections):
        priority = marker_priority(title)
        if priority is None:
            toc.append(title)
        else:
            ranked.append((priority, index, title, body))

    total = len(header) + sum(len(body) for _p, _i, _t, body in ranked)
    kept = header
    taken = len(header)
    dropped: list[str] = []
    for _priority, _index, title, body in sorted(ranked, key=lambda item: (item[0], item[1])):
        remaining = limit - taken
        if len(body) <= remaining:
            kept += body
            taken += len(body)
            continue
        # A section that only partly fits fills the budget instead of
        # wasting it: the tail is named, the coverage stays honest.
        if remaining >= MIN_PARTIAL:
            head = body[:remaining].rstrip("\n")
            marker = cut_marker(dropped_lines(body[remaining:]), source or "файл роли")
            kept += f"{head}\n{marker}\n"
            taken += len(head)
        dropped.append(title)

    pct = 100 if total <= 0 else min(100, (taken * 100) // total)
    lines = [f"- §{title} — {source or 'файл роли'}" for title in [*dropped, *toc]]
    result_text = kept.rstrip("\n")
    if lines:
        result_text = f"{result_text}\n\n## Не вошло в ядро\n" + "\n".join(lines)
    return RoleSelection(
        text=result_text,
        taken=taken,
        total=total,
        pct=pct,
        dropped=dropped,
        source=source,
    )


__all__ = [
    "HEADER_LIMIT",
    "MIN_PARTIAL",
    "ROLE_LOW_PCT",
    "ROLE_MARKERS",
    "RoleSelection",
    "marker_priority",
    "select_role_core",
]
