"""Findings of the fix round: the ``## REVIEW-фикс`` rounds, one owner (AC12).

``hygiene.review_append`` appends a ``## REVIEW-фикс (супервизор, виток N)``
section to the TODO per round; the reviewer's outbox report
(``REVIEW-REJECTED-<id>.md``) carries no such heading in this tree. The
rounds are read from the TODO through the pack's fence-aware walker
(:func:`awf._markdown.sections_with_prefix`), the report stays a fallback.

The last round is open, earlier ones are closed by the fix that followed:
their IDs become one ``закрыто: N (R-1, …)`` line and their bodies are not
retold (AC12). A ``BLOCKER`` line survives every cut (AC10/AC34).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import paths, signals
from .bounded_prompt import Block, fit_to_limit
from .stage_context import StageInput, todo_file, todo_text
from .stage_context import missing_block as _missing
from .stage_context import read_text as _read

#: A finding's own ID sits at the HEAD of its chunk: an ``R-N`` inside the
#: body is usually a reference, and the IDs of this tree are also ``RR40``.
_FINDING_HEAD = re.compile(r"^\s*(?:[-*+]\s+)?\*{0,2}([A-Z]{1,3}-?\d+)\b")

#: How a reviewer says "this one is done" (AC12).
_CLOSED = ("закрыто", "закрыт", "state: closed", "closed:")


def _chunks(text: str) -> list[str]:
    return [chunk.strip() for chunk in re.split(r"\n\s*\n", text) if chunk.strip()]


def _finding_ids(text: str) -> list[str]:
    return [match.group(1) for match in map(_FINDING_HEAD.match, _chunks(text)) if match]


def _rounds(text: str) -> list[str]:
    """Bodies of the ``## REVIEW-фикс`` rounds, oldest first.

    A round's body runs to the NEXT round heading, not to the next ``##``:
    the reviewer's report text is appended verbatim and carries its own
    level-2 headings (``## Stage facts``, ``## Что проверено``), so the
    pack's generic walker cut every body at its first inner heading.
    """
    from ._markdown import fence_flags

    lines = text.splitlines()
    flags = fence_flags(lines)
    heads = [
        index
        for index, line in enumerate(lines)
        if not flags[index] and line.startswith("## ") and line[3:].strip().startswith("REVIEW-фикс")
    ]
    bodies: list[str] = []
    for position, start in enumerate(heads):
        end = heads[position + 1] if position + 1 < len(heads) else len(lines)
        body = "\n".join(lines[start + 1 : end]).strip()
        if body:
            bodies.append(body)
    return bodies


def _owner(ctx: StageInput) -> tuple[Path | None, list[str]]:
    """``(source, rounds)``: the TODO first, the outbox report as fallback."""
    rounds = _rounds(todo_text(ctx))
    if rounds:
        return todo_file(ctx), rounds
    for candidate in signals.iter_review_reports(paths.outbox(ctx.project_dir), ctx.todo_id):
        rounds = _rounds(_read(candidate))
        if rounds:
            return candidate, rounds
    return None, []


def render_REVIEW(ctx: StageInput) -> Block:
    """Open findings of the last round, ``BLOCKER`` last (AC12, AC34)."""
    source, rounds = _owner(ctx)
    if source is None:
        return _missing("REVIEW", ctx, "секции находок нет", paths.outbox(ctx.project_dir))
    closed = [finding for older in rounds[:-1] for finding in _finding_ids(older)]
    open_chunks: list[str] = []
    blockers: list[str] = []
    for chunk in _chunks(rounds[-1]):
        lowered = chunk.lower()
        if any(mark in lowered for mark in _CLOSED):
            closed.extend(_finding_ids(chunk) or [""])
        elif "blocker" in lowered:
            blockers.append(chunk)
        else:
            open_chunks.append(chunk)
    body = "\n\n".join(open_chunks)
    tail: list[str] = []
    if closed:
        names = [name for name in dict.fromkeys(closed) if name]
        # The closed line is the ROUND's summary: it goes into the tail like
        # a BLOCKER, or a long open list cut it off the end of the block.
        tail.append(f"закрыто: {len(closed)}" + (f" ({', '.join(names)})" if names else ""))
    tail.extend(blockers)
    if not body and not tail:
        return _missing("REVIEW", ctx, "находок в секции нет", source)
    block = Block(
        id="REVIEW",
        text=body.strip(),
        limit=ctx.limit("REVIEW"),
        source=str(source),
        owner=str(source),
        tail="\n\n".join(tail),
    )
    return fit_to_limit(block)


__all__ = ["render_REVIEW"]
