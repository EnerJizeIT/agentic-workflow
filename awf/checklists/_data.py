"""Checklist data: the item schema, its strict validation and YAML loading.

A typo must never turn a check into a no-op: an unknown key, an unknown
probe type, an empty id or a shell-shaped command is a LOAD ERROR, not a
silently ignored line. The base/class files ship with the package; a wave
lives in the project (``.agentic/checklists/waves/<name>.yaml``).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .. import paths
from ._allowlist import argv_refusal, argv_structure_refusal, path_refusal

#: Item-level keys. A strict validator: an unknown key is an error, because
#: a mistyped ``probes:`` would otherwise drop the check on the floor.
ITEM_KEYS = ("id", "text", "probe", "expect", "classes")

#: The probe schema: type → the keys it carries. ONE table: the probe type
#: vocabulary below is derived from it, so a new type cannot be loaded while
#: missing from the key map (that was a KeyError instead of a refusal).
_PROBE_KEYS = {
    "grep_present": ("type", "file", "pattern"),
    "grep_absent": ("type", "file", "pattern"),
    "file_exists": ("type", "path"),
    "file_absent": ("type", "path"),
    "json_field": ("type", "file", "path"),
    "command_rc": ("type", "argv", "timeout"),
}

#: The probe vocabulary. Anything else is a load error — a typo must not
#: silently turn a check into a no-op.
PROBE_TYPES = tuple(_PROBE_KEYS)

MAX_PROBE_TIMEOUT = 120

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ChecklistError(ValueError):
    """A load/composition refusal — the message names the exact problem."""


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    probe: dict | None = None
    expect: object = None
    classes: tuple[str, ...] = ()
    source: str = "base"

    @property
    def manual(self) -> bool:
        return self.probe is None


# ─── validation ──────────────────────────────────────────────────────────


def _as_mapping(raw: object, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ChecklistError(f"{where}: expected a mapping, got {type(raw).__name__}")
    return raw


def _check_str(value: object, name: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ChecklistError(f"{where}: {name} must be a non-empty string")
    return value.strip()


def _check_probe_path(value: str, where: str) -> None:
    """A named probe path obeys the same "inside the project" rule as argv.

    The loaders refuse an escaping target at dispatch; the block parser and
    the gate repeat the check, because a hand-edited TODO is the same
    surface and the gate is the last step before a read.
    """
    refusal = path_refusal(value)
    if refusal:
        raise ChecklistError(f"{where}: {refusal}")


def _check_probe(raw: object, where: str) -> dict:
    probe = _as_mapping(raw, f"{where}.probe")
    kind = probe.get("type")
    if not isinstance(kind, str) or kind not in PROBE_TYPES:
        raise ChecklistError(
            f"{where}: probe.type must be one of {', '.join(PROBE_TYPES)} "
            f"(got {kind!r})"
        )
    allowed = _PROBE_KEYS[kind]
    extra = [k for k in probe if k not in allowed]
    if extra:
        raise ChecklistError(
            f"{where}: unknown probe key(s) {extra} for {kind} — allowed: "
            + ", ".join(allowed)
        )
    for key in allowed:
        if key == "type":
            continue
        if key == "timeout" and key not in probe:
            continue
        if key not in probe:
            raise ChecklistError(f"{where}: probe.{kind} requires {key!r}")
    if kind in ("grep_present", "grep_absent"):
        _check_str(probe["file"], "probe.file", where)
        _check_str(probe["pattern"], "probe.pattern", where)
        _check_probe_path(probe["file"], where)
    elif kind in ("file_exists", "file_absent"):
        _check_str(probe["path"], "probe.path", where)
        _check_probe_path(probe["path"], where)
    elif kind == "json_field":
        _check_str(probe["file"], "probe.file", where)
        _check_str(probe["path"], "probe.path", where)
        _check_probe_path(probe["file"], where)
    elif kind == "command_rc":
        # Shape only: no shell, an allowlisted binary. The CLOSED flag list
        # is argv_refusal's job — the loaders and the gate use it, so a data
        # file or a frozen block with `git --output` is refused before it can
        # run. parse_item stays the schema step; enforcement is layered on
        # top of it, never assumed from it.
        refusal = argv_structure_refusal(probe["argv"])
        if refusal:
            raise ChecklistError(f"{where}: {refusal}")
        timeout = probe.get("timeout", MAX_PROBE_TIMEOUT)
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise ChecklistError(f"{where}: probe.timeout must be an integer")
        if timeout < 1 or timeout > MAX_PROBE_TIMEOUT:
            raise ChecklistError(
                f"{where}: probe.timeout must be 1..{MAX_PROBE_TIMEOUT} seconds"
            )
        probe["timeout"] = timeout
    return probe


def parse_item(raw: object, where: str, *, source: str, classes: tuple[str, ...] = ()) -> Item:
    data = _as_mapping(raw, where)
    extra = [k for k in data if k not in ITEM_KEYS]
    if extra:
        raise ChecklistError(
            f"{where}: unknown key(s) {extra} — allowed: " + ", ".join(ITEM_KEYS)
        )
    item_id = data.get("id")
    if not isinstance(item_id, str) or not _ID_RE.match(item_id.strip()):
        raise ChecklistError(
            f"{where}: id must be a non-empty slug (letters, digits, '.', '_', '-')"
        )
    item_id = item_id.strip()
    text = _check_str(data.get("text"), "text", f"{where} ({item_id})")
    probe_raw = data.get("probe")
    probe = None if probe_raw is None else _check_probe(probe_raw, where)
    if probe is None:
        raise ChecklistError(f"{where} ({item_id}): data items require a probe")
    kind = probe["type"]
    expect = data.get("expect")
    if kind == "command_rc":
        if not isinstance(expect, int) or isinstance(expect, bool):
            raise ChecklistError(
                f"{where} ({item_id}): command_rc requires integer expect (the rc)"
            )
    elif kind == "json_field":
        if "expect" not in data:
            raise ChecklistError(
                f"{where} ({item_id}): json_field requires expect (the value)"
            )
        # The frozen block stores this value via ``json.dumps``; a YAML value
        # that is valid but not JSON-serialisable (an unquoted date, a set,
        # bytes) would raise a raw ``TypeError`` later, outside every caller's
        # refusal path. Refuse the data at load time instead.
        try:
            json.dumps(expect, sort_keys=True)
        except (TypeError, ValueError) as e:
            raise ChecklistError(
                f"{where} ({item_id}): json_field expect is not JSON: {e}"
            ) from None
    else:
        default = kind.endswith("_present") or kind == "file_exists"
        if expect is None:
            expect = default
        if not isinstance(expect, bool):
            raise ChecklistError(
                f"{where} ({item_id}): {kind} expect must be true/false"
            )
    declared = data.get("classes")
    if declared is not None:
        if not isinstance(declared, list) or not all(
            isinstance(c, str) and c for c in declared
        ):
            raise ChecklistError(f"{where} ({item_id}): classes must be a list of names")
        merged = tuple(dict.fromkeys((*classes, *declared)))
    else:
        merged = tuple(classes)
    return Item(
        id=item_id,
        text=text,
        probe=probe,
        expect=expect,
        classes=merged,
        source=source,
    )


# ─── loading ─────────────────────────────────────────────────────────────


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_yaml(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ChecklistError(f"cannot read {path}: {e}") from None
    except yaml.YAMLError as e:
        raise ChecklistError(f"{path} is not valid YAML: {e}") from None


def _strict_item(item: Item, where: str) -> Item:
    """The full allowlist for a DATA item — a bad flag refuses the dispatch.

    ``parse_item`` is the schema step (shape only); the YAML loaders add the
    closed flag/path verdict here, so a package or project data file cannot
    carry ``git --output`` or ``rg --pre`` into a unit block.
    """
    if item.probe and item.probe.get("type") == "command_rc":
        refusal = argv_refusal(item.probe["argv"])
        if refusal:
            raise ChecklistError(f"{where} ({item.id}): {refusal}")
    return item


def load_base(path: Path | None = None) -> list[Item]:
    """Load and validate the base set (``base.yaml`` by default)."""
    src = path or (_package_dir() / "base.yaml")
    data = _as_mapping(_load_yaml(src), str(src))
    extra = [k for k in data if k != "items"]
    if extra:
        raise ChecklistError(f"{src}: unknown top-level key(s) {extra}")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise ChecklistError(f"{src}: 'items' must be a list")
    return [
        _strict_item(
            parse_item(raw, f"{src.name}[{i}]", source="base"),
            f"{src.name}[{i}]",
        )
        for i, raw in enumerate(raw_items)
    ]


def load_classes(path: Path | None = None) -> dict[str, list[Item]]:
    """Load and validate the class catalog (``classes.yaml`` by default)."""
    src = path or (_package_dir() / "classes.yaml")
    data = _as_mapping(_load_yaml(src), str(src))
    extra = [k for k in data if k != "classes"]
    if extra:
        raise ChecklistError(f"{src}: unknown top-level key(s) {extra}")
    raw_classes = data.get("classes")
    if not isinstance(raw_classes, dict) or not raw_classes:
        raise ChecklistError(f"{src}: 'classes' must be a non-empty mapping")
    out: dict[str, list[Item]] = {}
    for name, raw_items in raw_classes.items():
        if not isinstance(name, str) or not _ID_RE.match(name):
            raise ChecklistError(f"{src}: bad class name {name!r}")
        if not isinstance(raw_items, list) or not raw_items:
            raise ChecklistError(f"{src}: class {name!r} must list its items")
        items = [
            _strict_item(
                parse_item(
                    raw,
                    f"{src.name}:{name}[{i}]",
                    source=f"class:{name}",
                    classes=(name,),
                ),
                f"{src.name}:{name}[{i}]",
            )
            for i, raw in enumerate(raw_items)
        ]
        out[name] = items
    return out


def wave_path(project_dir: str | Path, name: str) -> Path:
    """Where a project wave lives: ``.agentic/checklists/waves/<name>.yaml``."""
    return paths.waves_dir(project_dir) / f"{name}.yaml"


def load_wave(project_dir: str | Path, name: str) -> list[Item]:
    """Load one project wave. A missing or malformed file is a refusal."""
    if not isinstance(name, str) or not _ID_RE.match(name):
        raise ChecklistError(f"bad wave name {name!r} — expected a slug")
    path = wave_path(project_dir, name)
    if not path.is_file():
        raise ChecklistError(
            f"wave {name!r} not found at {path} — the wave file must exist "
            "before dispatch"
        )
    data = _as_mapping(_load_yaml(path), str(path))
    extra = [k for k in data if k not in ("name", "items")]
    if extra:
        raise ChecklistError(f"{path}: unknown top-level key(s) {extra}")
    declared = data.get("name")
    if declared is not None and declared != name:
        raise ChecklistError(
            f"{path}: wave declares name {declared!r}, expected {name!r}"
        )
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ChecklistError(f"{path}: 'items' must be a non-empty list")
    return [
        _strict_item(
            parse_item(raw, f"{path.name}[{i}]", source=f"wave:{name}"),
            f"{path.name}[{i}]",
        )
        for i, raw in enumerate(raw_items)
    ]


