"""Definition-of-done checklist: data, composition, render and parse.

The checklist is a verifiable list of completion criteria. It is composed
once, at dispatch, from four sources:

* the package base set (``base.yaml``) — the same for every unit;
* the classes the unit DECLARES (``checklist_classes:`` in the contract);
  boundary hints are printed only, never merged behind the unit's back;
* the wave items, when the contract names a wave (``wave: <name>``);
* the unit's own acceptance items (the ``## Приёмка`` section).

The composed block is frozen into the TODO (``## Чек-лист завершения``) —
a wave edited mid-unit does not change a live unit. The gate reads the
block back, so the TODO is the single source for what gets checked.

Ownership: base/classes are updated by the supervisor with the owner,
waves by the supervisor, the unit block is composed by dispatch; the
executor may only PROPOSE new items (a DONE section, never applied
automatically).

Modules: ``_data`` (schema + loaders), ``_allowlist`` (the closed
``command_rc`` flag list), ``_compose`` (composition), ``_render`` (block
render/parse), ``_boundaries`` (markdown section reads).
"""
from __future__ import annotations

from ._allowlist import argv_positionals, argv_refusal
from ._boundaries import PROPOSALS_NAME, section_lines
from ._compose import compose, compose_block, validate_carried_block
from ._data import (
    MAX_PROBE_TIMEOUT,
    ChecklistError,
    Item,
    load_base,
    load_classes,
    parse_item,
)
from ._render import (
    CHECKLIST_HEADING,
    CHECKLIST_NAME,
    block_has_items,
    block_hash,
    block_of,
    item_ids,
    parse_block,
    render_block,
    render_item,
    validate_readable_block,
)

__all__ = [
    "CHECKLIST_HEADING",
    "CHECKLIST_NAME",
    "ChecklistError",
    "Item",
    "MAX_PROBE_TIMEOUT",
    "PROPOSALS_NAME",
    "argv_positionals",
    "argv_refusal",
    "block_hash",
    "block_has_items",
    "block_of",
    "compose",
    "compose_block",
    "item_ids",
    "load_base",
    "load_classes",
    "parse_block",
    "parse_item",
    "render_block",
    "render_item",
    "section_lines",
    "validate_carried_block",
    "validate_readable_block",
]
