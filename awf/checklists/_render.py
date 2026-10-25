"""Render the composed block into the TODO and parse it back.

The TODO block is the single source the gate executes: it is frozen at
dispatch, so a later wave edit cannot change a live unit. The format is
one line per item, and a line the parser cannot read is a hard error — a
silently skipped checkbox is a check that never ran.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex

from ._allowlist import argv_refusal, path_refusal
from ._boundaries import level_two_count, section_with_heading
from ._data import (
    MAX_PROBE_TIMEOUT,
    PROBE_TYPES,
    ChecklistError,
    Item,
)

CHECKLIST_NAME = "Чек-лист завершения"
CHECKLIST_HEADING = f"## {CHECKLIST_NAME}"

#: One item line. The bullet is optional and may be any markdown marker —
#: ``- [ ]``, ``* [ ]``, ``+ [ ]`` and a bare ``[ ]`` all read, and leading
#: whitespace is harmless: an indented item is an item, not prose.
_ITEM_LINE = re.compile(r"^(?:[-*+]\s+)?\[ \] ([^:\s]+): (.*)$")


def _looks_like_checkbox(stripped: str) -> bool:
    """True for a line that TRIES to be a checkbox (never silently skipped)."""
    return stripped.startswith(("- [", "* [", "+ [", "["))


# ─── render / parse ─────────────────────────────────────────────────────


def _args_out(args: list[str]) -> str:
    # One grammar for the argv half: a JSON array. ``shlex.quote`` would
    # keep a tab, but a newline inside its single quotes is a PHYSICAL line
    # break in the line-oriented block (the S3 defect); JSON escapes it.
    return json.dumps(list(args), ensure_ascii=False)


def _text_out(value: str) -> str:
    # Item text may be multi-line: JSON string escaping keeps the item on
    # one physical line and ``_text_in`` reads it back exactly. A plain
    # single-line text stays plain — an old block reads the same either way,
    # so quoting is used only where it is needed to keep the line readable.
    ambiguous = (
        value.startswith('"')
        or " — probe(" in value
        or value.endswith(" — manual")
    )
    if (
        value
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
        and not ambiguous
    ):
        return value
    return json.dumps(value, ensure_ascii=False)


def _text_in(raw: str, item_id: str) -> str:
    raw = raw.strip()
    # Legacy/carried blocks render text as plain text; a quoted value is the
    # current (lossless) form. Both read, so an old frozen block still parses.
    if not raw.startswith('"'):
        return raw
    try:
        value = json.loads(raw)
    except ValueError:
        raise ChecklistError(f"unreadable item text in {item_id!r}") from None
    if not isinstance(value, str):
        raise ChecklistError(f"unreadable item text in {item_id!r}")
    return value


def _probe_spec(item: Item) -> str:
    assert item.probe is not None
    kind = item.probe["type"]
    if kind in ("grep_present", "grep_absent"):
        args = [item.probe["file"], item.probe["pattern"]]
    elif kind in ("file_exists", "file_absent"):
        args = [item.probe["path"]]
    elif kind == "json_field":
        args = [item.probe["file"], item.probe["path"]]
    else:
        args = list(item.probe["argv"])
    if kind == "command_rc":
        expect = f"rc={item.expect}"
    elif kind == "json_field":
        try:
            expect = json.dumps(item.expect, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as e:
            raise ChecklistError(f"json_field expect is not JSON in {item.id!r}: {e}") from None
    else:
        expect = "present" if item.expect else "absent"
    # The block is what the gate EXECUTES, so a declared timeout must survive
    # the freeze: a non-default bound is rendered in the stable slot right
    # after the probe type (never mixed into the argv), or it would silently
    # widen to the cap. The cap stays implicit — the common case reads plain.
    head = kind
    if kind == "command_rc":
        timeout = item.probe.get("timeout")
        if isinstance(timeout, int) and timeout != MAX_PROBE_TIMEOUT:
            head = f"{kind} timeout={timeout}"
    rendered = _args_out(args)
    return f"probe({head} {rendered}) expect {expect}"


def render_item(item: Item) -> str:
    tail = _probe_spec(item) if item.probe is not None else "manual"
    return f"- [ ] {item.id}: {_text_out(item.text)} — {tail}"


def render_block(items: list[Item]) -> str:
    """The TODO block, heading included and newline-terminated."""
    lines = [CHECKLIST_HEADING, ""]
    lines.extend(render_item(item) for item in items)
    return "\n".join(lines) + "\n"


def block_of(todo_text: str) -> str | None:
    """The raw ``## Чек-лист завершения`` block (heading + body), or None."""
    return section_with_heading(todo_text, CHECKLIST_NAME)


def block_has_items(todo_text: str) -> bool:
    """True when the block exists AND carries at least one item.

    A heading-only ``## Чек-лист завершения`` is not the frozen block: with
    zero items the unit would pass with zero checks (Q2/Q3). An unreadable
    block still counts as something to keep — it is carried, not dropped.
    ONE owner for the rule "a block is items, not a heading": ``dispatch``,
    ``hygiene`` and ``checklist apply`` all read it here.
    """
    if block_of(todo_text) is None:
        return False
    try:
        return bool(parse_block(todo_text))
    except ChecklistError:
        return True


def validate_readable_block(todo_text: str) -> None:
    """Refuse a FINAL text whose frozen block cannot be read back.

    ``compose_block`` already round-trips the block it renders, but the TODO
    is what the gate reads: a block appended after an unterminated code
    fence sits INSIDE the fence and reads as "no block" (``block_of`` →
    ``None``), so a unit dispatched WITH a DoD block would be green with
    zero checks. A second level-2 block is the other ambiguity — the gate
    executes the first while a human reads two. Both refuse here, before a
    byte reaches the TODO.
    """
    count = level_two_count(todo_text, CHECKLIST_NAME)
    if count == 0:
        raise ChecklistError(
            f"the final TODO has no readable '## {CHECKLIST_NAME}' block — an "
            "unterminated code fence swallows it; close the fence and "
            "dispatch again"
        )
    if count > 1:
        raise ChecklistError(
            f"the final TODO carries {count} '## {CHECKLIST_NAME}' blocks — "
            "the gate executes the first while a human reads two; keep "
            "exactly one"
        )
    parse_block(todo_text)


def item_ids(todo_text: str) -> list[str]:
    """Ids the block's item lines name, read WITHOUT the strict grammar.

    For a block the strict parser refused: the failure names the items that
    could not be checked instead of dying. Shares ``_ITEM_LINE`` with the
    parser, so the two readers can never disagree about what an item line
    is — a second regex here would be a second grammar.
    """
    block = block_of(todo_text)
    if block is None:
        return []
    ids: list[str] = []
    for line in block.splitlines()[1:]:
        match = _ITEM_LINE.match(line.strip())
        if match and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def block_hash(todo_text: str) -> str:
    """sha256 of exactly the block — the value frozen at dispatch (TODO-0245).

    The TODO is read with ``surrogateescape`` (a non-UTF-8 file is legitimate
    input), so the hash must encode the same way: a strict encode would raise
    on the very bytes the reader was built to keep.
    """
    block = block_of(todo_text)
    if block is None:
        return ""
    return hashlib.sha256(
        block.encode("utf-8", errors="surrogateescape")
    ).hexdigest()


def parse_block(todo_text: str) -> list[Item] | None:
    """Parse the TODO block back into items. None = no block.

    A line the parser cannot read is a hard error: a silently skipped
    checkbox is a check that never ran.
    """
    block = block_of(todo_text)
    if block is None:
        return None
    lines = block.splitlines()
    items: list[Item] = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # A checkbox-looking line is never dropped. Any bullet style and any
        # indentation reads (a human edit should not eat an item), and a line
        # that still does not match the item grammar is a hard error — a
        # silently skipped checkbox is a check that never ran.
        m = _ITEM_LINE.match(stripped)
        if not m:
            if _looks_like_checkbox(stripped):
                raise ChecklistError(f"unreadable checklist line: {line!r}")
            continue
        item_id, rest = m.group(1), m.group(2)
        if rest.endswith(" — manual"):
            items.append(
                Item(
                    id=item_id,
                    text=_text_in(rest[: -len(" — manual")], item_id),
                    source="block",
                )
            )
            continue
        marker = " — probe("
        if marker not in rest:
            raise ChecklistError(
                f"checklist line {item_id!r} has neither a probe nor 'manual'"
            )
        text, spec_tail = rest.rsplit(marker, 1)
        text = _text_in(text, item_id)
        spec = "probe(" + spec_tail
        pm = re.match(r"^probe\((.*)\) expect (.*)$", spec)
        if not pm:
            raise ChecklistError(f"unreadable probe line: {line!r}")
        hm = re.match(
            r"^([A-Za-z_][A-Za-z0-9_]*)(?: timeout=(\d+))?(?: (.*))?$",
            pm.group(1).strip(),
        )
        if not hm:
            raise ChecklistError(f"unreadable probe line: {line!r}")
        kind = hm.group(1)
        timeout_raw = hm.group(2)
        args = _split_args(hm.group(3) or "[]")
        expect_raw = pm.group(2).strip()
        probe, expect = _probe_from_tokens(
            kind, args, expect_raw, item_id, timeout_raw
        )
        items.append(
            Item(id=item_id, text=text, probe=probe, expect=expect, source="block")
        )
    return items


def _split_args(raw: str) -> list[str]:
    raw = raw.strip()
    # Current form: one JSON array (inverse of ``_args_out`` for every
    # character). Legacy/carried lines used shell quoting — still read.
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except ValueError as e:
            raise ChecklistError(f"unreadable probe arguments: {e}") from None
        if not isinstance(value, list) or not all(
            isinstance(a, str) for a in value
        ):
            raise ChecklistError("probe arguments must be a JSON list of strings")
        return value
    try:
        return shlex.split(raw)
    except ValueError as e:
        raise ChecklistError(f"unreadable probe arguments: {e}") from None


def _check_target(path: str, item_id: str) -> None:
    """A block-declared path obeys the project rule (same as the data load)."""
    refusal = path_refusal(path)
    if refusal:
        raise ChecklistError(f"{item_id}: {refusal}")


def _check_nonempty(value: str, what: str, item_id: str) -> None:
    """The block schema is not weaker than the YAML one (`_data._check_str`).

    An empty grep pattern matches every line and an empty path resolves to
    the project root: both answered PASS while checking nothing.
    """
    if not value.strip():
        raise ChecklistError(f"probe {what} must be non-empty ({item_id})")


def _present_or_absent(expect_raw: str, kind: str, item_id: str) -> bool:
    """The block's expect token is schema-checked, like a bool in YAML.

    ``expect nonsense`` used to read as ``absent``: a typo in a hand-edited
    line silently inverted the check.
    """
    if expect_raw not in ("present", "absent"):
        raise ChecklistError(
            f"probe {kind} expect must be 'present' or 'absent' ({item_id})"
        )
    return expect_raw == "present"


def _probe_from_tokens(
    kind: str,
    args: list[str],
    expect_raw: str,
    item_id: str,
    timeout_raw: str | None = None,
) -> tuple[dict, object]:
    if kind not in PROBE_TYPES:
        raise ChecklistError(f"unknown probe type {kind!r} in {item_id!r}")
    if kind in ("grep_present", "grep_absent"):
        if len(args) != 2:
            raise ChecklistError(f"probe {kind} needs file + pattern ({item_id})")
        _check_nonempty(args[0], "file", item_id)
        _check_nonempty(args[1], "pattern", item_id)
        _check_target(args[0], item_id)
        expect = _present_or_absent(expect_raw, kind, item_id)
        probe = {"type": kind, "file": args[0], "pattern": args[1]}
    elif kind in ("file_exists", "file_absent"):
        if len(args) != 1:
            raise ChecklistError(f"probe {kind} needs one path ({item_id})")
        _check_nonempty(args[0], "path", item_id)
        _check_target(args[0], item_id)
        expect = _present_or_absent(expect_raw, kind, item_id)
        probe = {"type": kind, "path": args[0]}
    elif kind == "json_field":
        if len(args) != 2:
            raise ChecklistError(f"probe json_field needs file + path ({item_id})")
        _check_nonempty(args[0], "file", item_id)
        _check_nonempty(args[1], "path", item_id)
        _check_target(args[0], item_id)
        probe = {"type": kind, "file": args[0], "path": args[1]}
        try:
            expect = json.loads(expect_raw)
        except ValueError:
            raise ChecklistError(
                f"probe json_field expect is not JSON in {item_id!r}"
            ) from None
    else:  # command_rc
        timeout = MAX_PROBE_TIMEOUT
        if timeout_raw is not None:
            timeout = int(timeout_raw)
            if not 1 <= timeout <= MAX_PROBE_TIMEOUT:
                raise ChecklistError(
                    f"probe command_rc timeout must be 1..{MAX_PROBE_TIMEOUT} "
                    f"({item_id})"
                )
        if not expect_raw.startswith("rc="):
            raise ChecklistError(f"probe command_rc expects rc=<int> ({item_id})")
        try:
            expect = int(expect_raw[3:])
        except ValueError:
            raise ChecklistError(f"probe command_rc rc is not an int ({item_id})")
        # The block is the single source the gate EXECUTES. A hand-edited
        # block must pass the same allowlist as the YAML data — otherwise
        # the freeze step would be the only guard and a `bash -c` line in a
        # TODO would run at verify time.
        refusal = argv_refusal(args)
        if refusal:
            raise ChecklistError(f"{item_id}: {refusal}")
        probe = {"type": kind, "argv": list(args), "timeout": timeout}
    return probe, expect


__all__ = [
    "CHECKLIST_HEADING",
    "CHECKLIST_NAME",
    "block_hash",
    "block_has_items",
    "block_of",
    "item_ids",
    "parse_block",
    "render_block",
    "render_item",
    "validate_readable_block",
]
