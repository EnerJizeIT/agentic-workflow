"""Markdown section helpers shared by the API submodules and ``checklists``.

M5 introduced two readers of the same shape: ``dispatch`` takes a named
section from a source-of-truth file, ``hygiene`` takes the findings and
fix directions from a BLOCKED note. Both delimit a section by headings, so
the heading primitives live here instead of next to either caller; turning
list items into checkboxes stays with ``dispatch`` (its only consumer).

TODO-0244: the checklist package reads the unit's ``Границы``/``Приёмка``
sections and counts the frozen heading with the same walker. The module is
dependency-free, so it lives at the package root — importing it through
``awf.api`` would execute the whole application layer (dispatch, hygiene,
verify_pack) just to read a heading, and make the data package depend on
its consumer.

One fence-aware line walker serves all of them: a ``#``-line inside a
fenced code block is a comment, not a heading, and a ``- key: value`` line
there is an example, not a requirement. Keeping the fence rule in one
place means the section reader and the checklist writer cannot disagree.

The same module owns the two line-level questions a unit file asks about
its own shape: where the leading comments / front matter end
(:func:`skip_leading_trivia`) and where the first real heading of a given
level is (:func:`first_heading_index`). They live together because the
answer to "is this line a heading" must be one answer — a body heading
inside a fence or a front-matter key is not the title.
"""
from __future__ import annotations

import re

#: A heading line: 1-6 ``#`` + its text.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")

#: An opening or closing code fence: 3+ backticks or tildes.
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")


def fence_flags(lines: list[str]) -> list[bool]:
    """Mark every line that belongs to a fenced code block.

    A fence opens on a run of three or more backticks or tildes and closes
    on the same character with a run at least as long (a longer fence may
    wrap a shorter one); anything else inside the block is opaque text.
    Both the opening and the closing line are flagged, so a caller skips
    them as one unit. An unterminated fence flags the rest of the text —
    the same way a renderer treats it.
    """
    flags: list[bool] = []
    fence_char = ""
    fence_len = 0
    for line in lines:
        m = FENCE_RE.match(line)
        if not fence_char:
            if m:
                fence_char = m.group(2)[0]
                fence_len = len(m.group(2))
                flags.append(True)
            else:
                flags.append(False)
            continue
        if (
            m
            and m.group(2)[0] == fence_char
            and len(m.group(2)) >= fence_len
            and not m.group(3).strip()
        ):
            fence_char = ""
            fence_len = 0
        flags.append(True)
    return flags


def skip_leading_trivia(lines: list[str]) -> int:
    """Index of the first line after the leading comments, blanks and front
    matter.

    A unit file may open with ``<!-- role_hint: ... -->`` (optionally several
    lines), then the ``---`` contract block. Neither is content: the title
    follows them. An unterminated comment or contract block consumes the
    rest of the file — the same way the parsers see it.
    """
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("<!--"):
            while index < len(lines) and "-->" not in lines[index]:
                index += 1
            index += 1
            continue
        if not stripped:
            index += 1
            continue
        break
    if index < len(lines) and lines[index].strip() == "---":
        end = index + 1
        while end < len(lines) and lines[end].strip() != "---":
            end += 1
        if end < len(lines):
            index = end + 1
    return index


def first_heading_index(
    lines: list[str], *, level: int = 1, start: int = 0
) -> int | None:
    """Index of the first heading of exactly ``level`` at/after ``start``.

    Fenced lines are skipped: a ``#``-line in an example is not a heading.
    ``None`` means the file has no such heading — the caller must then
    treat the title as absent instead of falling back to a deeper heading
    (a body ``##`` section is not the document title).
    """
    flags = fence_flags(lines)
    for index in range(start, len(lines)):
        if flags[index]:
            continue
        m = HEADING_RE.match(lines[index])
        if m and len(m.group(1)) == level:
            return index
    return None


def find_headings(text: str, name: str) -> list[tuple[int, int]]:
    """Every ``(line index, level)`` whose heading text is exactly ``name``.

    The match is exact (case-insensitive) — no first-word fallback: an
    ambiguous name must be refused by the caller, never resolved to the
    first hit behind its back. Headings inside a fenced code block do not
    count.
    """
    want = name.strip().casefold()
    lines = text.splitlines()
    hits: list[tuple[int, int]] = []
    for index, (line, in_fence) in enumerate(zip(lines, fence_flags(lines))):
        if in_fence:
            continue
        m = HEADING_RE.match(line)
        if m and m.group(2).strip().casefold() == want:
            hits.append((index, len(m.group(1))))
    return hits


def lines_below(text: str, index: int, level: int) -> list[str]:
    """Lines after heading ``index`` up to the next heading of the same or a
    higher level — nested sub-sections stay inside, fence lines included."""
    lines = text.splitlines()
    flags = fence_flags(lines)
    body: list[str] = []
    for line, in_fence in zip(lines[index + 1:], flags[index + 1:]):
        if not in_fence:
            m = HEADING_RE.match(line)
            if m and len(m.group(1)) <= level:
                break
        body.append(line)
    return body


def append_block(text: str, block: str) -> str:
    """Put a markdown block at the END of a text: one blank line, one newline.

    ONE spelling for every writer of a block — the DoD block at dispatch,
    ``checklist apply`` and the reword carry, and a REVIEW/BLOCKED section.
    Before this helper each writer joined the strings itself, and one of
    them dropped the trailing newline: the same file ended differently
    depending on who wrote it last. Only the outer edges are normalised;
    the block's own lines stay byte-for-byte what the caller produced.
    """
    return text.rstrip("\n") + "\n\n" + block.strip("\n") + "\n"


def section_body(text: str, name: str, *, level: int = 2) -> str:
    """Body of the ``## <name>`` section, up to the next heading of the
    same or a higher level.

    Only a heading of exactly ``level`` starts collection; deeper headings
    stay inside the captured body. Unlike :func:`find_headings` this keeps
    the level filter (a BLOCKED note's ``## НАХОДКИ`` is a fixed level-2
    key) and collects the body text at once instead of its line indexes.
    Fences only affect which lines are comments, not what the body holds:
    a fenced example (a quoted ``## НАХОДКИ`` heading included) is data and
    stays in the body, because the walker already ignores its headings.
    Repeated keys (an older note appended twice) contribute their bodies in
    order rather than being refused: the BLOCKED schema has one key, and a
    merged body loses nothing.
    """
    chunks: list[str] = []
    for index, found in find_headings(text, name):
        if found != level:
            continue
        chunks.append("\n".join(lines_below(text, index, found)))
    return "\n".join(chunks).strip()


__all__ = [
    "HEADING_RE",
    "append_block",
    "fence_flags",
    "find_headings",
    "first_heading_index",
    "lines_below",
    "section_body",
    "skip_leading_trivia",
]
