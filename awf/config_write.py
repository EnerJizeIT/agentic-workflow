"""Comment-preserving patcher for ``.agentic/config.yaml`` (R10, K10).

The setup form applied its ``models.<role>`` updates by loading the whole
file with PyYAML and dumping it back — every comment, blank line and the
original key order were lost. This module patches only the scalar lines of
the ``models:`` block.

Contract (:func:`patch_mapping`):

- only the changed ``agent_name`` / ``model`` scalars of the named roles
  are touched; comments, blank lines and the order of every other key keep
  their bytes. Two exceptions, both only when the whole section is missing
  and gets appended: the blank lines at the end of the file are trimmed,
  and a file without a final newline gains one;
- a missing key is inserted into the role block, a ``None``/``""`` value
  removes the key;
- the edit is validated by parsing the result and comparing it with the
  expected mapping; a mismatch, broken YAML, a flow-style ``models:`` block
  or a non-standard indent raises :class:`AwfApiError` and the caller does
  not write the file;
- a second identical call returns the input unchanged (idempotent).

:func:`patch_config_values` applies the same byte-preserving edit to any
other top-level section (``context.message``, ``supervisor.instructions``):
the UI-2/UI-3 values may be multi-line, so a replaced or removed scalar
takes its continuation lines with it.
"""
from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from typing import Any

import yaml

from ._errors import AwfApiError

_MISSING = object()
_PATCHABLE_KEYS = ("agent_name", "model")
_SCALAR_KEY_RE = re.compile(r"^(?P<key>[^\s:#][^:]*):(?P<rest>.*)$")
_EMPTY_FLOW_ROLE_RE = re.compile(r"^\{\s*\}\s*(#.*)?$")


def _section_line_re(section: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(section)}:\s*(#.*)?$")


def _section_empty_flow_re(section: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(section)}:\s*\{{\s*\}}\s*(#.*)?$")


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _scalar(value: str) -> str:
    """Canonical single-line YAML scalar (round-trips through safe_load).

    The dump width is unbounded: PyYAML folds a plain scalar at 80 columns,
    and taking the first line of a folded dump truncates the value — a normal
    sentence in ``context.message`` then failed the round-trip check and the
    whole patch was refused.
    """
    for line in yaml.safe_dump(
        value, default_flow_style=False, allow_unicode=True, width=1 << 30
    ).splitlines():
        line = line.strip()
        if line and line != "...":
            return line
    return '""'


def _text_scalar(value: Any) -> str:
    """Single-line YAML scalar for arbitrary text.

    A one-line value keeps the canonical plain/quoted form (so a simple
    ``hi`` stays ``hi``). ``_scalar`` alone is wrong for multi-line user
    text — it keeps the first line of a PyYAML dump — so such a value is
    written as a JSON string, which is a valid YAML double-quoted scalar
    on one line.
    """
    text = str(value)
    if "\n" not in text and "\r" not in text:
        return _scalar(text)
    return json.dumps(text, ensure_ascii=False)


def _split_comment(rest: str) -> tuple[str, str]:
    """Split ``<value>  # comment`` respecting quoted scalars."""
    quote = ""
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote:
            if ch == quote:
                if quote == "'" and rest[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = ""
            elif ch == "\\" and quote == '"':
                i += 2
                continue
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            start = i
            while start > 0 and rest[start - 1] in " \t":
                start -= 1
            return rest[:start].strip(), rest[start:].rstrip()
        i += 1
    return rest.strip(), ""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses duplicate mapping keys."""


def _construct_no_dup_mapping(loader, node):
    seen = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in seen:
            raise yaml.YAMLError(f"duplicate key {key!r} in mapping")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=False)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_no_dup_mapping,
)


def _load(text: str, *, operation: str) -> dict[str, Any]:
    try:
        data = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as exc:
        raise AwfApiError(
            f"config.yaml: {operation} refused — invalid YAML: {exc}"
        ) from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise AwfApiError(
            f"config.yaml: {operation} refused — root is not a mapping"
        )
    return data


def _find_block(
    lines: list[str], section_idx: int, section: str, *, operation: str
) -> tuple[int, int, int, int]:
    """Return ``(block_end, entry_indent, child_indent, empty_flow)``.

    The block ends at the next column-0 non-comment line. ``empty_flow``
    is 1 when the header was ``<section>: {}`` (rewritten to a block
    form), else 0. A line indented deeper than the child indent is a
    continuation of the previous scalar — not a third indentation level.
    """
    header = lines[section_idx]
    empty_flow = bool(_section_empty_flow_re(section).match(header))

    block_end = len(lines)
    for i in range(section_idx + 1, len(lines)):
        line = lines[i]
        if _is_noise(line):
            continue
        if _line_indent(line) == 0:
            block_end = i
            break

    role_indent: int | None = None
    child_indent: int | None = None
    for i in range(section_idx + 1, block_end):
        line = lines[i]
        if _is_noise(line):
            continue
        leading = line[: len(line) - len(line.lstrip())]
        if "\t" in leading:
            raise AwfApiError(
                f"config.yaml: {operation} refused — tab indentation "
                f"in the {section} block"
            )
        indent = _line_indent(line)
        if role_indent is None or indent < role_indent:
            if role_indent is not None:
                raise AwfApiError(
                    f"config.yaml: {operation} refused — non-standard "
                    f"indentation in the {section} block"
                )
            role_indent = indent
        elif role_indent == indent:
            continue
        elif child_indent is None:
            child_indent = indent
        # deeper than child_indent: a multi-line scalar continuation

    if role_indent is None:
        role_indent = 2
    if role_indent <= 0 or role_indent % 2:
        raise AwfApiError(
            f"config.yaml: {operation} refused — non-standard indentation "
            f"in the {section} block"
        )
    if child_indent is None:
        child_indent = role_indent + 2
    if child_indent != role_indent + 2:
        raise AwfApiError(
            f"config.yaml: {operation} refused — non-standard indentation "
            f"in the {section} block"
        )
    return block_end, role_indent, child_indent, int(empty_flow)


def _scan_entries(
    lines: list[str],
    section_idx: int,
    block_end: int,
    role_indent: int,
    child_indent: int,
    *,
    section: str,
    operation: str,
    scalar_values: bool = False,
) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    i = section_idx + 1
    while i < block_end:
        line = lines[i]
        if _is_noise(line):
            i += 1
            continue
        indent = _line_indent(line)
        if indent != role_indent:
            raise AwfApiError(
                f"config.yaml: {operation} refused — non-standard "
                f"indentation in the {section} block"
            )
        match = _SCALAR_KEY_RE.match(line.strip())
        rest = match.group("rest").strip() if match else ""
        # A role written as ``role: {}`` (empty mapping) is accepted: the
        # writer itself produces that form when the last key is removed, so
        # refusing it would make its own output unreadable.
        empty_flow_role = bool(rest and _EMPTY_FLOW_ROLE_RE.match(rest))
        if not match or (
            not scalar_values
            and rest
            and not rest.startswith("#")
            and not empty_flow_role
        ):
            raise AwfApiError(
                f"config.yaml: {operation} refused — {section} entries must "
                "be plain mappings"
            )
        role = match.group("key").strip()
        # PA-0286-2: a quoted key (``"dev":``) parses to the same name as its
        # unquoted form. The line scanner keys entries by the raw text, so an
        # update for ``dev`` would miss ``"dev"`` and insert a second ``dev:``
        # — the round-trip check then failed with a misleading "duplicate
        # key" error. Refuse the form up front with a clear message (the
        # caller leaves the file untouched).
        if role[:1] in ('"', "'"):
            raise AwfApiError(
                f"config.yaml: {operation} refused — quoted key not "
                f"supported in the {section} block"
            )
        role_end = block_end
        for j in range(i + 1, block_end):
            if not _is_noise(lines[j]) and _line_indent(lines[j]) <= role_indent:
                role_end = j
                break
        children: dict[str, int] = {}
        for j in range(i + 1, role_end):
            child = lines[j]
            if _is_noise(child):
                continue
            if scalar_values:
                # The entry itself is a leaf scalar; every deeper line is
                # its continuation.
                continue
            if _line_indent(child) > child_indent:
                # Continuation of the previous scalar (a multi-line
                # ``instructions`` value written by PyYAML).
                if not children:
                    raise AwfApiError(
                        f"config.yaml: {operation} refused — nested values "
                        f"are not supported in the {section} block"
                    )
                continue
            if _line_indent(child) != child_indent:
                raise AwfApiError(
                    f"config.yaml: {operation} refused — nested values are "
                    f"not supported in the {section} block"
                )
            child_match = _SCALAR_KEY_RE.match(child.strip())
            if not child_match:
                raise AwfApiError(
                    f"config.yaml: {operation} refused — malformed key in the "
                    f"{section} block"
                )
            children[child_match.group("key").strip()] = j
        roles[role] = {
            "header": i,
            "end": role_end,
            "children": children,
            "empty_flow": empty_flow_role,
        }
        i = role_end
    return roles


def _header_with_value(line: str, role: str, value: str) -> str:
    """Rewrite a role header, keeping indentation and its trailing comment."""
    rest = line.strip()[len(role) + 1 :]
    _old, comment = _split_comment(rest)
    sep = "" if comment.startswith((" ", "\t")) else " "
    return (
        f"{' ' * _line_indent(line)}{role}:{value}"
        + (f"{sep}{comment}" if comment else "")
    )


def _scalar_end(
    lines: list[str], idx: int, limit: int, child_indent: int
) -> int:
    """Index after the last line of the scalar that starts at *idx*.

    A line indented deeper than ``child_indent`` continues the scalar
    (PyYAML wraps long values); a comment or blank line inside it belongs
    to the same value.
    """
    end = idx + 1
    last_continuation = idx + 1
    while end < limit:
        line = lines[end]
        if _is_noise(line):
            end += 1
            continue
        if _line_indent(line) > child_indent:
            end += 1
            last_continuation = end
            continue
        break
    return last_continuation


def _patched_line(
    lines: list[str], idx: int, key: str, value: str, child_indent: int
) -> str:
    """Rewrite a ``key:`` line, keeping indentation and its trailing comment."""
    rest = lines[idx].strip()[len(key) + 1 :]
    _old, comment = _split_comment(rest)
    sep = "" if comment.startswith((" ", "\t")) else " "
    return (
        f"{' ' * child_indent}{key}: {value}"
        + (f"{sep}{comment}" if comment else "")
    )


def _apply_edits(
    lines: list[str], edits: list[tuple[int, int, str | None]]
) -> int:
    """Apply ``(start, end, new_line)`` edits bottom-up; return the line delta."""
    delta = 0
    for start, end, new_line in sorted(edits, key=lambda e: e[0], reverse=True):
        if new_line is None:
            del lines[start:end]
            delta -= end - start
        else:
            lines[start] = new_line
            del lines[start + 1 : end]
            delta -= end - start - 1
    return delta


def _last_content_end(lines: list[str], start: int, end: int) -> int:
    """Index after the last non-noise line in ``[start, end)``, else *start*."""
    for j in range(end - 1, start - 1, -1):
        if not _is_noise(lines[j]):
            return j + 1
    return start


def _patch_children(
    lines: list[str],
    info: dict[str, Any],
    target: dict[str, Any],
    current: dict[str, Any],
    child_indent: int,
    *,
    keys: tuple[str, ...],
    serialize: Any,
) -> None:
    """Apply the target children of one mapping entry (caller rescans)."""
    children: dict[str, int] = info["children"]
    inserts: list[str] = []
    edits: list[tuple[int, int, str | None]] = []

    for key in keys:
        wanted = target.get(key, _MISSING)
        idx = children.get(key)
        if wanted is _MISSING and idx is None:
            continue
        if key in current and current[key] == wanted:
            continue
        if wanted is _MISSING:
            edits.append(
                (idx, _scalar_end(lines, idx, info["end"], child_indent), None)
            )
            continue
        value = serialize(wanted)
        if idx is None:
            inserts.append(f"{' ' * child_indent}{key}: {value}")
            continue
        end = _scalar_end(lines, idx, info["end"], child_indent)
        edits.append(
            (idx, end, _patched_line(lines, idx, key, value, child_indent))
        )

    edited = _apply_edits(lines, edits)

    # A flow-style empty entry (``role: {}``) has no block to insert into:
    # switch the header back to block form, keeping its trailing comment.
    if info.get("empty_flow") and inserts:
        lines[info["header"]] = _header_with_value(
            lines[info["header"]], info["name"], ""
        )

    # Insert missing keys after the last content line of the entry block.
    insert_at = _last_content_end(
        lines, info["header"] + 1, info["end"] + edited
    )
    if inserts:
        lines[insert_at:insert_at] = inserts
    delta = edited + len(inserts)

    # Comments are not mapping content: an entry left with only comments is
    # an empty mapping, not a ``null`` value.
    remaining = [
        j
        for j in range(info["header"] + 1, info["end"] + delta)
        if not _is_noise(lines[j])
    ]
    if not target and not remaining:
        lines[info["header"]] = _header_with_value(
            lines[info["header"]], info["name"], " {}"
        )


def _append_section(
    lines: list[str],
    section: str,
    target: Mapping[str, Any],
    serialize: Any,
    *,
    scalar_values: bool,
) -> None:
    while lines and not lines[-1].strip():
        lines.pop()
    if lines:
        lines.append("")
    lines.append(f"{section}:")
    for name, entry in target.items():
        if scalar_values:
            lines.append(f"  {name}: {serialize(entry)}")
            continue
        lines.append(f"  {name}:")
        for key, value in entry.items():
            lines.append(f"    {key}: {serialize(value)}")


def _patch_section(
    text: str,
    section: str,
    updates: Mapping[str, Any],
    *,
    operation: str,
    scalar_values: bool,
) -> str:
    """Patch the scalars of one top-level *section*, keeping every other byte."""
    if not updates:
        return text

    # AUD4: the line model is LF-only. Rewriting one line of a CRLF file
    # would strip its \r while its neighbours keep theirs — refuse instead
    # of corrupting the formatting.
    if "\r" in text:
        raise AwfApiError(
            f"config.yaml: {operation} refused — CRLF line endings are not "
            "supported; the file was left untouched"
        )

    config = _load(text, operation=operation)
    current_section = config.get(section)
    if current_section is None:
        current_section = {}
    if not isinstance(current_section, dict):
        raise AwfApiError(
            f"config.yaml: {operation} refused — {section!r} is not a mapping"
        )

    target_section: dict[str, Any] = copy.deepcopy(current_section)
    for name, change in updates.items():
        if scalar_values:
            if change is None or change == "":
                target_section.pop(name, None)
            else:
                target_section[name] = str(change)
            continue
        if not isinstance(change, Mapping):
            raise AwfApiError(
                f"config.yaml: {operation} refused — update for {name!r} "
                "is not a mapping"
            )
        unknown = [key for key in change if key not in _PATCHABLE_KEYS]
        if unknown:
            raise AwfApiError(
                f"config.yaml: {operation} refused — unsupported key(s) "
                f"{unknown} for {name!r}"
            )
        current_entry = current_section.get(name)
        if current_entry is not None and not isinstance(current_entry, dict):
            raise AwfApiError(
                f"config.yaml: {operation} refused — {section}.{name} "
                "is not a mapping"
            )
        entry = dict(current_entry or {})
        for key, value in change.items():
            if value is None or value == "":
                entry.pop(key, None)
            else:
                entry[key] = str(value)
        target_section[name] = entry

    if target_section == current_section:
        return text

    serialize = _text_scalar if scalar_values else (lambda v: _scalar(str(v)))
    lines = text.split("\n")
    section_idx = next(
        (
            i
            for i, line in enumerate(lines)
            if _section_line_re(section).match(line)
            or _section_empty_flow_re(section).match(line)
        ),
        None,
    )

    if section_idx is None:
        if section in config:
            raise AwfApiError(
                f"config.yaml: {operation} refused — flow-style or "
                f"indented {section!r} key is not supported"
            )
        _append_section(
            lines,
            section,
            target_section,
            serialize,
            scalar_values=scalar_values,
        )
    else:
        block_end, entry_indent, child_indent, empty_flow = _find_block(
            lines, section_idx, section, operation=operation
        )
        if empty_flow:
            # AUD0286-1: keep the trailing comment of ``models: {}  # x`` —
            # the sibling paths all go through ``_header_with_value``.
            lines[section_idx] = _header_with_value(
                lines[section_idx], section, ""
            )
        entries = _scan_entries(
            lines,
            section_idx,
            block_end,
            entry_indent,
            entry_indent if scalar_values else child_indent,
            section=section,
            operation=operation,
            scalar_values=scalar_values,
        )
        leaf_edits: list[tuple[int, int, str | None]] = []
        leaf_inserts: list[tuple[str, Any]] = []
        if scalar_values:
            for name in updates:
                wanted = target_section.get(name, _MISSING)
                info = entries.get(name)
                if info is None:
                    if wanted is not _MISSING:
                        leaf_inserts.append((name, wanted))
                    continue
                end = _scalar_end(
                    lines, info["header"], info["end"], entry_indent
                )
                if wanted is _MISSING:
                    leaf_edits.append((info["header"], end, None))
                else:
                    leaf_edits.append(
                        (
                            info["header"],
                            end,
                            f"{' ' * entry_indent}{name}: {serialize(wanted)}",
                        )
                    )
            leaf_delta = _apply_edits(lines, leaf_edits)
            insert_at = _last_content_end(
                lines, section_idx + 1, block_end + leaf_delta
            )
            if leaf_inserts:
                lines[insert_at:insert_at] = [
                    f"{' ' * entry_indent}{name}: {serialize(value)}"
                    for name, value in leaf_inserts
                ]
            leaf_delta += len(leaf_inserts)
            remaining = [
                j
                for j in range(section_idx + 1, block_end + leaf_delta)
                if not _is_noise(lines[j])
            ]
            if not target_section and not remaining:
                lines[section_idx] = _header_with_value(
                    lines[section_idx], section, " {}"
                )
        for name in updates:
            if scalar_values:
                continue
            if name in entries:
                info = dict(entries[name], name=name)
                _patch_children(
                    lines,
                    info,
                    target_section[name],
                    current_section.get(name) or {},
                    child_indent,
                    keys=_PATCHABLE_KEYS,
                    serialize=serialize,
                )
            else:
                insert_at = _last_content_end(lines, section_idx + 1, block_end)
                if target_section[name]:
                    block = [f"{' ' * entry_indent}{name}:"]
                    for key, value in target_section[name].items():
                        block.append(
                            f"{' ' * child_indent}{key}: {serialize(value)}"
                        )
                else:
                    block = [f"{' ' * entry_indent}{name}: {{}}"]
                lines[insert_at:insert_at] = block

            # Rescan: indices shift after the patched entry.
            block_end, entry_indent, child_indent, _ = _find_block(
                lines, section_idx, section, operation=operation
            )
            entries = _scan_entries(
                lines,
                section_idx,
                block_end,
                entry_indent,
                child_indent,
                section=section,
                operation=operation,
            )

    new_text = "\n".join(lines)
    # Appending a section trims the trailing blank lines, so the file would
    # lose its final newline — an extra byte outside the patched keys.
    # AUD0286-3: a missing/empty input grows a fresh file, which must end
    # with a newline too (POSIX text-file convention).
    if (not text or text.endswith("\n")) and not new_text.endswith("\n"):
        new_text += "\n"

    expected = copy.deepcopy(config)
    expected[section] = target_section
    got = _load(new_text, operation=operation)
    if got != expected:
        raise AwfApiError(
            f"config.yaml: {operation} refused — the result does not match "
            "the requested mapping; the file was left untouched"
        )
    return new_text


def patch_mapping(
    text: str, updates: Mapping[str, Mapping[str, Any]]
) -> str:
    """Patch ``models.<role>.<key>`` scalars in *text* and return the result.

    *updates* maps a role slug to the keys that change. A value of ``None``
    or ``""`` removes the key; any other value is written as a scalar. An
    unchanged mapping returns *text* itself (idempotent).

    Raises :class:`AwfApiError` for broken YAML, a flow-style ``models:``
    block, non-standard indentation, unsupported keys or a result that does
    not round-trip to the expected mapping. The caller must not write the
    file when this raises.
    """
    return _patch_section(
        text,
        "models",
        updates,
        operation="models patch",
        scalar_values=False,
    )


def patch_config_values(
    text: str,
    updates: Mapping[str, Mapping[str, Any]],
    *,
    operation: str = "config values",
) -> str:
    """Patch ``<section>.<key>`` scalars, keeping comments and order (UI-2/3).

    *updates* maps a top-level section to the scalars that change. Multi-line
    values are written as one quoted line and take the old value's
    continuation lines with them. A ``None``/``""`` value removes the key.
    An unchanged mapping returns *text* itself (idempotent).

    Same refusals as :func:`patch_mapping`; the caller must not write the
    file when this raises.
    """
    for section, change in updates.items():
        if not change:
            continue
        if not isinstance(change, Mapping):
            raise AwfApiError(
                f"config.yaml: {operation} refused — update for {section!r} "
                "is not a mapping"
            )
        text = _patch_section(
            text,
            section,
            change,
            operation=f"{operation}: {section}",
            scalar_values=True,
        )
    return text
