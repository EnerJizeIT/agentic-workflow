"""Section readers for the checklist composer and for the gate.

Kept apart from the composition logic so the markdown rules live in one
place (``awf._markdown`` is the shared, dependency-free reader — at the
package root, so this data package does not import the application layer).
"""
from __future__ import annotations

from .._markdown import find_headings, lines_below, section_body

#: The DONE-report section the executor proposes items in. It belongs to a
#: section READER (the gate), not to the TODO block renderer: the gate reads
#: this heading off ``DONE-<todo>.md``.
PROPOSALS_NAME = "Предложения по чек-листу"


def boundaries_text(todo_text: str) -> str:
    """The body of the unit's ``## Границы`` section (empty when absent)."""
    return section_body(todo_text, "Границы")


def section_lines(todo_text: str, name: str) -> list[str]:
    """Body lines of ``## <name>`` — fence lines stay as data."""
    return section_body(todo_text, name).splitlines()


def level_two_count(todo_text: str, name: str) -> int:
    """How many LEVEL-2 headings carry exactly ``name`` (fence-aware)."""
    return sum(1 for _, level in find_headings(todo_text, name) if level == 2)


def section_with_heading(todo_text: str, name: str) -> str | None:
    """The exact block: the heading line AS WRITTEN, body to the next heading.

    Only the first occurrence is returned (a doubled block is a broken
    TODO, not a merge candidate); trailing blank lines are trimmed so a
    re-render produces the same hash. The heading line is returned verbatim,
    not rebuilt from ``name``: callers replace the block by exact substring
    (``checklist apply``, the reword carry) and hash it for the cache, so a
    rebuilt heading would miss a line that differs only in trailing spaces
    and turn the write into a silent no-op.
    """
    hits = find_headings(todo_text, name)
    level_two = [index for index, level in hits if level == 2]
    if not level_two:
        return None
    index = level_two[0]
    lines = todo_text.splitlines()
    body = lines_below(todo_text, index, 2)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join([lines[index], *body])


__all__ = [
    "PROPOSALS_NAME",
    "boundaries_text",
    "level_two_count",
    "section_lines",
    "section_with_heading",
]
