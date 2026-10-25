"""Composition: base + declared classes + wave + unit acceptance.

Declared classes only. Boundary hints are printed, never merged: a unit
that needs a class says so in its contract. Duplicate ids across the four
sources are a refusal — one id names exactly one check.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ._boundaries import boundaries_text, section_lines
from ._data import ChecklistError, Item, load_base, load_classes, load_wave
from ._render import parse_block, render_block


@dataclass
class Composition:
    items: list[Item] = field(default_factory=list)
    suggested: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    wave: str = ""


# ─── composition ────────────────────────────────────────────────────────


def merge_items(
    base: list[Item],
    classes: dict[str, list[Item]],
    declared: list[str],
    wave_items: list[Item],
    unit_items: list[Item],
) -> list[Item]:
    """Compose the item list. Declared classes only; duplicate ids are refused."""
    unknown = [c for c in declared if c not in classes]
    if unknown:
        raise ChecklistError(
            "unknown checklist class(es) "
            + ", ".join(repr(c) for c in unknown)
            + " — known: "
            + ", ".join(sorted(classes))
        )
    items: list[Item] = list(base)
    for name in declared:
        items.extend(classes[name])
    items.extend(wave_items)
    items.extend(unit_items)
    seen: dict[str, str] = {}
    for item in items:
        first = seen.get(item.id)
        if first is not None:
            raise ChecklistError(
                f"duplicate checklist id {item.id!r} — declared by {first} and "
                f"{item.source}; ids must be unique across base, classes, wave "
                "and unit acceptance"
            )
        seen[item.id] = item.source
    return items


#: Boundary keywords → class names. A hint only: the unit decides. This
#: table is the one suggester; the renderer prints the names, never merges.
_SUGGESTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fs-writer", ("file", "файл", "запис", "writer", "atomic", "managed")),
    ("encoding", ("encoding", "кодиров", "utf-8", "utf8", "surrogateescape")),
    ("cli-parity", ("cli", "argparse", "команд", "subcommand", "сабпарс")),
    ("scripts", ("scripts/", "bash", "shell-скрипт", "run-all", ".sh")),
    ("roles", ("roles/", "роль", "role", "роли")),
    ("packaging", ("pyproject", "package", "packaging", "упаковк")),
)


def suggest_classes(todo_text: str) -> list[str]:
    """Classes suggested by the unit's ``## Границы`` section (hints only)."""
    text = boundaries_text(todo_text).casefold()
    if not text:
        return []
    return [
        name
        for name, words in _SUGGESTIONS
        if any(word.casefold() in text for word in words)
    ]


def acceptance_lines(todo_text: str) -> list[str]:
    """The unit's acceptance items — one per non-empty line of ``## Приёмка``.

    Bullets lose their marker; indented continuation lines (the ``(a)``
    block under a file item) stay separate items: each line is one
    verifiable statement, and a merged blob is not.
    """
    out: list[str] = []
    for line in section_lines(todo_text, "Приёмка"):
        stripped = line.strip()
        if not stripped:
            continue
        m = re.match(r"^[-*+]\s+(.*)$", stripped)
        out.append((m.group(1) if m else stripped).strip())
    return out


def unit_items(todo_text: str) -> list[Item]:
    """Acceptance lines as manual checklist items (``unit-01``, ``unit-02``...)."""
    return [
        Item(id=f"unit-{i:02d}", text=text, probe=None, source="unit")
        for i, text in enumerate(acceptance_lines(todo_text), 1)
    ]


def _declared(contract: dict) -> tuple[list[str], str]:
    """The contract's announcement: declared classes (stripped) and the wave."""
    declared = contract.get("checklist_classes") or []
    if not isinstance(declared, list) or not all(isinstance(c, str) for c in declared):
        raise ChecklistError("'checklist_classes' must be a list of class names")
    wave_name = contract.get("wave") or ""
    if wave_name and not isinstance(wave_name, str):
        raise ChecklistError("'wave' must be a wave name")
    return [c.strip() for c in declared], wave_name


def validate_carried_block(
    project_dir: str | Path,
    todo_text: str,
    contract: dict | None = None,
) -> list[Item]:
    """A carried TODO brings its own frozen block — check the declaration.

    The block itself is NOT recomposed (it is frozen at dispatch by design,
    so a wave edited mid-unit cannot change it). But the contract still
    ANNOUNCES classes and a wave, and a carried copy (a replan) must not
    silently drop a declared class: an unknown class, a missing wave file or
    a declared class whose items are absent from the block is a refusal.
    """
    contract = contract or {}
    declared, wave_name = _declared(contract)
    expected = merge_items([], load_classes(), declared, [], [])
    items = parse_block(todo_text) or []
    if not items:
        raise ChecklistError(
            "the carried checklist block carries no items — a heading is not "
            "a block (remove it or fence it as an example); dispatch will "
            "compose the real block"
        )
    ids = {item.id for item in items}
    missing = [item.id for item in expected if item.id not in ids]
    if missing:
        raise ChecklistError(
            "carried checklist block misses declared class item(s) "
            + ", ".join(repr(item_id) for item_id in missing)
            + " — the declaration and the frozen block must agree"
        )
    if wave_name:
        load_wave(project_dir, wave_name)
    return items


def compose(
    project_dir: str | Path,
    todo_text: str,
    contract: dict | None = None,
) -> Composition:
    """Compose the checklist for a unit about to be dispatched.

    Raises :class:`ChecklistError` on an unknown class, a missing wave, a
    duplicate id or an invalid probe — the dispatch refuses the unit.
    """
    contract = contract or {}
    declared, wave_name = _declared(contract)

    base = load_base()
    catalog = load_classes()
    wave_items = load_wave(project_dir, wave_name) if wave_name else []
    items = merge_items(
        base, catalog, declared, wave_items, unit_items(todo_text)
    )
    # A hint is an INVITATION to declare a class; a class the unit already
    # declared is not a hint. Filtering here gives dispatch and
    # ``checklist apply`` one meaning of ``suggested`` (the second used to
    # suppress the whole line when any class was declared).
    #
    # The catalog is the vocabulary: ``_SUGGESTIONS`` names classes as
    # strings, so a renamed or removed class would leave a hint that points
    # at a class ``merge_items`` refuses — the supervisor would be told to
    # declare something that does not exist. Hints are drawn from the loaded
    # catalog only; a stale name disappears instead of misleading.
    suggested = [
        c for c in suggest_classes(todo_text) if c in catalog and c not in declared
    ]
    return Composition(
        items=items,
        suggested=suggested,
        classes=declared,
        wave=wave_name,
    )


def compose_block(
    project_dir: str | Path,
    todo_text: str,
    contract: dict | None = None,
) -> tuple[Composition, str]:
    """Compose AND render — refuse a block the parser cannot read back.

    ONE gate for every writer of the frozen block (dispatch, ``checklist
    apply``). The block is the single source the gate EXECUTES, so a
    renderer/parser drift must refuse before a byte reaches the TODO. The
    check is a full re-render, not just the ids: text, probe argv and the
    expected value must survive the round trip exactly, or a future codec
    change would freeze a checkbox the gate can never read.
    """
    composition = compose(project_dir, todo_text, contract)
    block = render_block(composition.items)
    try:
        parsed = parse_block("# TODO\n\n" + block)
    except ChecklistError as e:
        raise ChecklistError(f"rendered block is unreadable: {e}") from None
    if parsed is None or render_block(parsed).strip() != block.strip():
        raise ChecklistError(
            "rendered block does not round-trip — refusing to freeze an "
            "unreadable block"
        )
    return composition, block


