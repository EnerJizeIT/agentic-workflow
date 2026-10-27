"""CARRY IMP2 (R13/K14): one builder for the canonical full-suite command.

The engine and ``scripts/suite-canonical.sh`` must ask the same question and
get the same string, otherwise the artifact the script writes never matches
the command the engine looks for. This module is the single literal home of
the legacy pytest fallback.

Resolution (K14):

- ``verification.test_cmd`` is a non-empty string → ``source="config"``,
  the command exactly as written;
- the key is absent → ``source="legacy"`` with the pytest fallback, the
  interpreter taken from ``AWF_PYTHON`` (or ``python3``);
- ``null``, an empty/whitespace string, broken YAML or a non-mapping
  config → ``None`` ("suite is not configured").

``AWF_SUITE_CMD`` is NOT read here: only the shell script honours that
override, and the engine never treats such an artifact as canonical
(AC34).

The module also answers a pytest-config question the gate needs:
``testpaths_configured`` — see the block below ``resolve``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import config as cfg_mod
from . import paths

CONFIG_SOURCE = "config"
LEGACY_SOURCE = "legacy"

LEGACY_TEMPLATE = (
    "{pybin} -m pytest tests/ -q -n auto --timeout=300 -p no:cacheprovider"
)


@dataclass(frozen=True)
class SuiteCommand:
    """A resolved canonical command and where it came from."""

    command: str
    source: str


def legacy_command() -> str:
    """The pytest fallback for projects with no ``verification.test_cmd``."""
    pybin = os.environ.get("AWF_PYTHON") or "python3"
    return LEGACY_TEMPLATE.format(pybin=pybin)


def _load_config(project_dir: str | Path) -> tuple[dict, str]:
    """Return ``(config, state)`` with ``state`` in ``ok/missing/broken``.

    ``config.load`` hides a malformed file behind ``{}`` (right for every
    tolerant caller), but here "no key" and "cannot read the file" must stay
    apart: the first is a legacy fallback, the second is "not configured".
    """
    cfg_path = paths.config_file(project_dir)
    if not cfg_path.exists():
        return {}, "missing"
    try:
        with cfg_path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return {}, "broken"
    if data is None:
        # An empty or comment-only file is valid YAML with no keys: K14
        # puts "no key" on the legacy path, not on "unreadable" (QA-0277 F6).
        return {}, "ok"
    if not isinstance(data, dict):
        return {}, "broken"
    return data, "ok"


def resolve_with_state(
    project_dir: str | Path = ".",
) -> tuple[SuiteCommand | None, str]:
    """Resolve the command and name the branch it took (CTX1, volut 4).

    States: ``config`` / ``legacy`` (a command), ``unset`` (a null, an
    empty or a non-string value) and ``broken`` (the YAML does not parse).
    The CLI returns a separate exit code for ``broken``: «the file cannot
    be read» must not look like «the key is empty» — K14 keeps them apart.
    """
    config, state = _load_config(project_dir)
    if state == "broken":
        return None, "broken"
    missing = object()
    value = cfg_mod.get(config, "verification.test_cmd", missing)
    if state == "missing" or value is missing:
        return SuiteCommand(legacy_command(), LEGACY_SOURCE), LEGACY_SOURCE
    if value is None:
        return None, "unset"
    if not isinstance(value, str) or not value.strip():
        return None, "unset"
    return SuiteCommand(value, CONFIG_SOURCE), CONFIG_SOURCE


def resolve(project_dir: str | Path = ".") -> SuiteCommand | None:
    """Resolve the canonical full-suite command, or ``None`` when unset."""
    return resolve_with_state(project_dir)[0]


# --- pytest config facts (CARRY IMP2, AUD1) --------------------------------
#
# ``testpaths_configured`` answers one question the gate needs beside the
# canonical command: does the project's pytest config declare a non-empty
# ``testpaths``? A bare ``pytest`` counts as a full run only then (owner's
# AUD1 rule); ``suite_guard`` asks, this module only reads files. It lives
# here because reading project config is this module's job, and because
# ``suite_guard`` stays a pure log-text classifier: no file I/O there.
#
# The scan is line by line: ``tomllib`` is 3.11+, the package supports
# 3.10. Every file pytest itself reads is checked (including the 8.1+
# ``pytest.toml``/``.pytest.ini``) and the answers are OR-ed: pytest follows
# only the highest-precedence file, but the gate prefers a spurious warning
# over a missed raw run.

_PYTEST_SECTIONS = {
    "pyproject.toml": ("[tool.pytest.ini_options]",),
    "pytest.ini": ("[pytest]",),
    "tox.ini": ("[pytest]",),
    "setup.cfg": ("[tool:pytest]",),
    # pytest 8.1+ reads these too (pytest 9 lists all seven names).
    "pytest.toml": ("[pytest]",),
    ".pytest.toml": ("[pytest]",),
    ".pytest.ini": ("[pytest]",),
}
#: TOML has no significant indentation: a value that closes on its line is
#: complete, and the next indented line is a NEW key. Only ini continues a
#: value onto indented lines, so only ini files get that rule.
_TOML_FILES = frozenset({"pyproject.toml", "pytest.toml", ".pytest.toml"})
_QUOTED = re.compile(r"""["']([^"']*)["']""")
_TEXT = re.compile(r"[^\s,\[\]\"']+")


def _strip_comment(line: str) -> str:
    """Drop a ``#`` comment outside quotes (AUD12).

    TOML and ini both start a comment at ``#``; a ``#`` inside a quoted
    value is data (``testpaths = ["a#b"]``).
    """
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _section_value(
    lines: list[str],
    sections: tuple[str, ...],
    key: str,
    *,
    indented: bool,
) -> str:
    """The raw value of *key* in one of *sections* (continuations joined).

    Two continuation shapes are real, and each belongs to one format. A TOML
    array closes on a later line (``testpaths = [`` … ``]``) — bracket
    balance, never indentation. An ini value spills onto indented lines
    (CTX3, ``indented=True``); iniconfig reads them as one value. Applying
    the ini rule to TOML would swallow an indented NEXT key and invent a
    ``testpaths`` that is not there.
    """
    section = None
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = _strip_comment(raw).strip()
        i += 1
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section not in sections or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() != key:
            continue
        parts = [value]
        if "[" in value and "]" not in value:
            # TOML array: keep going until the closing bracket.
            while i < len(lines):
                nxt = _strip_comment(lines[i]).strip()
                i += 1
                parts.append(nxt)
                if "]" in nxt:
                    break
            return "\n".join(parts)
        if not indented:
            return value  # TOML: the line is the whole value
        # ini continuation: indented lines below the key belong to the value.
        while i < len(lines):
            nxt_raw = lines[i]
            nxt = _strip_comment(nxt_raw).strip()
            if not nxt:
                i += 1  # a blank line neither adds nor ends a value
                continue
            if not nxt_raw[:1].isspace():
                break  # an unindented key/section ends the value
            parts.append(nxt)
            i += 1
        return "\n".join(part for part in parts if part)
    return ""


def testpaths_configured(project_dir: str | Path) -> bool:
    """True when pytest config declares a non-empty ``testpaths`` (AUD1)."""
    root = Path(project_dir)
    for name, sections in _PYTEST_SECTIONS.items():
        path = root / name
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        value = _section_value(
            lines, sections, "testpaths", indented=name not in _TOML_FILES
        )
        if any(part.strip() for part in _QUOTED.findall(value)):
            return True
        # ini spelling: ``testpaths = tests unit`` carries no quotes.
        if _TEXT.findall(value):
            return True
    return False


__all__ = [
    "CONFIG_SOURCE",
    "LEGACY_SOURCE",
    "LEGACY_TEMPLATE",
    "SuiteCommand",
    "legacy_command",
    "resolve",
    "resolve_with_state",
    "testpaths_configured",
]
