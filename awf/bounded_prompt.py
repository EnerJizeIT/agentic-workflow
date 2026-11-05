"""A42/K8: the one place where a text that is over budget gets cut.

Two shapes obey the same rules: the stage context builder
(``stage_context.build_stage_context``) hands over named blocks, and
``fit_prompt`` still serves named sections. IMP2b moved every live prompt
to the builder, so the rules live here once instead of next to each caller:

- sections are named pairs ``(label, text)`` and the caller keeps the
  formatting: ``render(items)`` produces the whole prompt, head and tail
  included, so the fixed instructions are part of every measurement;
- ``required`` sections always go in — they are the reason the prompt
  exists (a verdict, a reviewer report);
- each ``optional`` section enters only when the WHOLE render still fits,
  otherwise it is dropped by name;
- overflow is then taken off the sections named in ``truncate_order``, BY
  LABEL — never off the end of the prompt, because the tail carries the
  closing instruction (``replan_for:`` / the signal to write) and cutting
  it produces an answer no waiter recognizes;
- every cut is named in the returned list, and the caller logs that list;
- the last resort — the fixed text alone (instructions and section frames)
  is over the budget — truncates the tail and names itself differently, so
  a section cut is never mistaken for it.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

Section = tuple[str, str]
Renderer = Callable[[list[Section]], str]

#: R2 classes: P0 is never cut, P5 goes first.
P0, P1, P2, P3, P4, P5 = 0, 1, 2, 3, 4, 5

#: The block's class (arch §2). One table for the fitter and for a reader
#: that asks "may this block be cut at all".
BLOCK_PRIORITY: dict[str, int] = {
    "STAGE": P0,
    "UNIT": P0,
    "CRIT": P0,
    "CHECK": P0,
    "PHASES": P0,
    "RULES": P0,
    "OUTPUT": P0,
    # QA-11/vitok 5: the findings are why the round exists — never cut.
    "REVIEW": P0,
    "DONE": P1,
    "INPUTART": P1,
    "ROLE": P2,
    "GATES": P3,
    "DIFF": P3,
    "BLOCKED": P3,
    "DOCTRINE": P4,
    "STATE": P4,
    "VISION": P4,
    "PREV": P5,
    "PREVLINK": P5,
}

#: R5 cut order: P5 first, then the facts/narrative; ``ROLE`` LAST so a unit
#: whose contract fills the cap does not pay with the role (QA-11). P0 blocks
#: (``REVIEW`` included since vitok 5) are never in this list — they are the
#: reason the stage runs.
CUT_ORDER: tuple[str, ...] = (
    "PREVLINK",
    "PREV",
    "DOCTRINE",
    "STATE",
    "VISION",
    "GATES",
    "DIFF",
    "BLOCKED",
    "INPUTART",
    "DONE",
    "ROLE",
)

#: Vitok 5: the stage tail is static guidance; the unit text keeps the rest.
SUFFIX_MAX = 1500

#: P0 blocks that still honour their own share: the findings keep the tail
#: (``BLOCKER``/closed) and let the body go. P0 for the cut order means
#: "never dropped by the cap", not "never trimmed".
SHARED_P0: frozenset[str] = frozenset({"REVIEW"})

#: First guess of what the cut marker costs: callers add the real marker
#: length on top (it carries the artifact path), so this only seeds the cut.
MARKER_RESERVE = 80


@dataclass
class Block:
    """One rendered block of a stage text.

    ``text`` is the cuttable part, ``tail`` survives every cut (a REVIEW's
    ``BLOCKER`` lines), ``missing`` names why the artifact was absent —
    such a block is never a P0 failure.
    """

    id: str
    text: str = ""
    priority: int = P0
    limit: int = 0
    source: str = ""
    owner: str = ""
    tail: str = ""
    missing: str = ""
    cut: int = 0
    #: ROLE only: the characters of the role's core that went in and its
    #: whole core — the numbers ``role_pct`` is computed from. They are
    #: fields, not text parsed back out of ``owner``: a renamed label must
    #: never change a measurement.
    taken: int = 0
    total: int = 0

    def __post_init__(self) -> None:
        if self.id in BLOCK_PRIORITY:
            self.priority = BLOCK_PRIORITY[self.id]

    def full(self) -> str:
        return self.text + self.tail


def cut_marker(lines: int, source: str) -> str:
    """AC10: the one spelling of "the rest is in the file"."""
    where = source or "исходный файл"
    return f"… ещё {lines}: см. {where}"


def dropped_lines(dropped: str) -> int:
    """How many non-empty lines a cut removed (never zero: a cut happened)."""
    return max(1, sum(1 for line in dropped.split("\n") if line.strip()))


def cut_text(text: str, keep: int, source: str) -> str:
    """*text* trimmed to *keep* chars plus the marker naming the rest."""
    keep = max(0, min(keep, len(text)))
    dropped = text[keep:]
    if not dropped:
        return text
    marker = cut_marker(dropped_lines(dropped), source)
    kept = text[:keep].rstrip("\n")
    return f"{kept}\n{marker}" if kept else marker


def cut_block(block: Block, keep: int) -> str:
    """*block*'s cuttable text trimmed to *keep* chars plus the marker."""
    return cut_text(block.text, keep, block.source)


def fit_to_limit(block: Block) -> Block:
    """Cut *block* to its own ``limit``, marker included (K8, arch §6).

    P0 and a missing artifact are never cut — P0 is the reason the stage
    runs, and a missing block is one line. The caller decides which blocks
    take part: ``PREVLINK`` is exempt because AC5 wants every other handoff
    named by path, and its share is the budget, not a hard clip.
    """
    # REVIEW is P0 for the cut order (never dropped by the cap), but its own
    # share still applies: tail (BLOCKER/closed) survives, body gets a marker.
    if (block.priority == P0 and block.id not in SHARED_P0) or block.missing or block.limit <= 0:
        return block
    if len(block.full()) <= block.limit:
        return block
    # The marker carries the ARTIFACT PATH, so its length is not a constant:
    # a fixed reserve cut a block to ``limit`` and then parked a 100-char path
    # on top of it (DONE 406/400, REVIEW 389/350), which is exactly the share
    # the cap is summed from. Take the real marker off and repeat while it
    # still overflows; at keep=0 the block is the marker alone.
    room = block.limit - len(block.tail)
    keep = max(0, room - MARKER_RESERVE)
    text = cut_block(block, keep)
    while len(text) + len(block.tail) > block.limit and keep > 0:
        keep = max(0, keep - (len(text) + len(block.tail) - block.limit))
        text = cut_block(block, keep)
    block.text = text
    return block


def _cut_by(block: Block, overflow: int) -> tuple[str, int]:
    """Cut *block*'s text so that at least *overflow* characters leave it.

    The marker costs characters: a cut computed as ``len(text) - overflow``
    gives back a little less than asked, and the residue would make the fit
    fail with ``cap`` after every soft block was already cut. The loop takes
    the difference off the same block — ``block.text`` stays whole until the
    caller assigns, so the marker is never doubled.
    """
    keep = max(0, len(block.text) - overflow)
    text = cut_block(block, keep)
    removed = len(block.full()) - len(text + block.tail)
    while removed < overflow and keep > 0:
        keep = max(0, keep - (overflow - removed))
        text = cut_block(block, keep)
        removed = len(block.full()) - len(text + block.tail)
    return text, removed


def _joined_len(blocks: Sequence[Block]) -> int:
    """Length of the text :func:`fit_blocks` will return, separators included.

    The blocks are joined with a blank line, so a cap checked on the plain
    sum would let the real text pass it by two characters per joint.
    """
    parts = [block.full() for block in blocks if block.full()]
    return sum(len(part) for part in parts) + 2 * max(0, len(parts) - 1)


def fit_blocks(blocks: Sequence[Block], cap: int) -> tuple[str, list[str], list[str]]:
    """K8: the text of *blocks*, cut to *cap* by :data:`CUT_ORDER`.

    Returns ``(text, cut ids, failed ids)``. A missing block is a line, not
    a failure. A block's own limit is a target for the soft classes only: a
    P0 block is never cut, so its real size may pass its share — what P0 may
    not do is overflow *cap* itself. Then its text is withheld (``""``) and
    the caller refuses the stage (D4, AC3/AC10).
    """
    failed: list[str] = []
    p0 = [block for block in blocks if block.priority == P0 and not block.missing]
    if _joined_len(p0) > cap:
        failed.append("P0")
    if failed:
        return "", [], failed

    overflow = _joined_len(blocks) - cap
    by_id = {block.id: block for block in blocks}
    cut: list[str] = []
    for block_id in CUT_ORDER:
        block = by_id.get(block_id)
        if overflow <= 0:
            break
        if block is None or not block.full() or block.missing:
            # AC9: a missing artifact's one line ("нет данных: причина —
            # путь") is the only record that the input was absent — cutting
            # it left a bare marker and told the stage nothing.
            continue
        new_text, removed = _cut_by(block, overflow)
        if removed <= 0:
            continue
        block.text = new_text
        block.cut += removed
        overflow -= removed
        cut.append(block_id)

    text = "\n\n".join(block.full() for block in blocks if block.full())
    if overflow > 0:
        return "", cut, ["cap"]
    return text, cut, []


def minimum_blocks_len(blocks: Sequence[Block], floor: int = 0) -> int:
    """The smallest text :func:`fit_blocks` can produce for *blocks*.

    Every soft block keeps a marker naming its artifact, a missing block
    keeps its one line, P0 is never cut, and ``ROLE`` is only promised down
    to *floor*. A caller that reserves room for a tail must leave this many
    characters, or the fitter takes the rest off the role it promised.
    """
    parts = [block for block in blocks if block.full()]
    total = 2 * max(0, len(parts) - 1)
    for block in parts:
        if block.missing:
            total += len(block.full())
        elif block.id == "ROLE" and floor:
            total += min(floor, len(block.full()))
        elif block.priority:
            total += len(cut_marker(1, block.source))
        else:
            total += len(block.full())
    return total


def fit_prompt(
    render: Renderer,
    *,
    required: Sequence[Section],
    optional: Sequence[Section],
    budget: int,
    truncate_order: Sequence[str],
) -> tuple[str, list[str]]:
    """Render ``required`` + fitting ``optional`` sections inside ``budget``.

    Returns ``(prompt, dropped)``. Empty texts are skipped: a missing source
    is an absent section, never an empty heading.
    """
    dropped: list[str] = []
    kept: list[Section] = [(label, text) for label, text in required if text]

    for label, text in optional:
        if not text:
            continue
        if len(render([*kept, (label, text)])) <= budget:
            kept.append((label, text))
        else:
            dropped.append(label)

    prompt = render(kept)
    if len(prompt) > budget:
        overflow = len(prompt) - budget
        for wanted in truncate_order:
            if overflow <= 0:
                break
            idx = next(
                (i for i, (label, _text) in enumerate(kept) if label == wanted),
                None,
            )
            if idx is None:
                continue
            label, text = kept[idx]
            keep = max(0, len(text) - overflow)
            if keep < len(text):
                dropped.append(f"{label} truncated")
                overflow -= len(text) - keep
            kept[idx] = (label, text[:keep])
        prompt = render(kept)

    if len(prompt) > budget:
        # The fixed text (instructions and section frames) alone is over the
        # budget: a limit too small to carry this prompt at all. The tail
        # gives way and the fact is named differently from a section cut.
        dropped.append("fixed text truncated")
        prompt = prompt[:budget]
    return prompt, dropped


__all__ = [
    "BLOCK_PRIORITY",
    "CUT_ORDER",
    "MARKER_RESERVE",
    "SUFFIX_MAX",
    "Block",
    "cut_block",
    "cut_marker",
    "cut_text",
    "dropped_lines",
    "fit_blocks",
    "fit_prompt",
    "fit_to_limit",
    "minimum_blocks_len",
]
