"""U3: unit contract — optional machine-readable block in a TODO + DONE.json.

TODO front-matter (optional, at the very top of the TODO file, between two
``---`` lines; leading ``<!-- ... -->`` comment lines and blank lines are
skipped when locating the block)::

    verify: ["python3 -m pytest tests/unit/test_x.py -q"]
    gates: ["contracts", "ratchet"]
    prove_red: ["tests/unit/test_x.py::test_y"]

``dispatch_todo`` validates the block; a TODO without a block works exactly
as before. Broken YAML / wrong types / empty lists / unknown gate names are
hard errors (AwfApiError at the API level); unknown keys are warnings only.

DONE.json (optional, written by the worker to ``.agentic/outbox/``)::

    {"files_changed": [...],
     "tests_run": [{"cmd": "...", "result": "..."}],
     "gates": [...], "notes": "..."}

``collect_handoff`` folds a valid DONE.json into the handoff as a
"Machine facts (DONE.json)" section; a broken or schema-violating file is a
log warning and the handoff continues without the section (never a crash).
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from ._log import log as _log
from .signals import find_signal_file

#: Gate names ``scripts/run-all.sh`` can run: the fixed checks plus the
#: project-commands entries, under their short names.
KNOWN_GATES: frozenset[str] = frozenset(
    {"contracts", "ratchet", "instructions", "tests", "lint", "mutations"}
)

_CONTRACT_KEYS: tuple[str, ...] = ("verify", "gates", "prove_red")


def _find_contract_lines(content: str) -> tuple[list[str] | None, str | None]:
    """Locate the contract block. Returns (yaml_lines, error).

    (None, None) — no block; (lines, None) — block found; (None, error) —
    block opened but broken.
    """
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s or s.startswith("<!--"):
            i += 1
            continue
        break
    if i >= len(lines) or lines[i].strip() != "---":
        return None, None
    for j in range(i + 1, len(lines)):
        if lines[j].strip() == "---":
            return lines[i + 1 : j], None
    return None, (
        "contract block opened with `---` at the top but no closing `---` "
        "found — close the block (one `---` line after the last key)"
    )


def _check_str_list(value: object, field: str) -> None:
    """Field must be a non-empty list of non-empty strings."""
    if not isinstance(value, list):
        raise ValueError(
            f"'{field}' must be a list of command strings, "
            f'e.g. {field}: ["python3 -m pytest tests/ -q"]'
        )
    if not value:
        raise ValueError(
            f"'{field}' must not be empty — drop the key if there is nothing to declare"
        )
    for k, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"'{field}[{k}]' must be a non-empty string, got {item!r}")


def parse_todo_contract(content: str) -> tuple[dict | None, list[str]]:
    """Parse + validate the optional contract block at the top of a TODO.

    Returns ``(contract, unknown_keys)``: ``contract`` is ``None`` when the
    TODO has no block, otherwise a dict (possibly empty). Raises
    ``ValueError`` with a message naming the problem and the fix.
    """
    yaml_lines, error = _find_contract_lines(content)
    if error:
        raise ValueError(error)
    if yaml_lines is None:
        return None, []

    try:
        data = yaml.safe_load("\n".join(yaml_lines))
    except yaml.YAMLError as e:
        raise ValueError(f"contract block is not valid YAML: {e}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(
            f"contract block must be a YAML mapping with keys {list(_CONTRACT_KEYS)}, "
            f"got {type(data).__name__}"
        )

    unknown = [k for k in data if k not in _CONTRACT_KEYS]
    if "verify" in data:
        _check_str_list(data["verify"], "verify")
    if "gates" in data:
        _check_str_list(data["gates"], "gates")
        for g in data["gates"]:
            if g.strip() not in KNOWN_GATES:
                raise ValueError(
                    f"unknown gate '{g.strip()}' — allowed: "
                    + ", ".join(sorted(KNOWN_GATES))
                )
    if "prove_red" in data:
        _check_str_list(data["prove_red"], "prove_red")
    return data, unknown


def parse_done_json(text: str) -> dict | None:
    """Parse + validate DONE.json. Returns a dict, or None when broken.

    Schema (all keys optional): ``files_changed: [str]``,
    ``tests_run: [{cmd: str, result: str}]``, ``gates: [str]``,
    ``notes: str``.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("files_changed", "gates"):
        v = data.get(key)
        if v is not None and (
            not isinstance(v, list) or not all(isinstance(x, str) for x in v)
        ):
            return None
    tests_run = data.get("tests_run")
    if tests_run is not None:
        if not isinstance(tests_run, list):
            return None
        for item in tests_run:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("cmd"), str)
                or not isinstance(item.get("result"), str)
            ):
                return None
    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        return None
    return data


def render_done_json(data: dict) -> list[str]:
    """Render a validated DONE.json dict as handoff markdown lines."""
    lines: list[str] = []
    files_changed = data.get("files_changed")
    if files_changed:
        listed = ", ".join(f"`{p}`" for p in files_changed[:10])
        more = f" … +{len(files_changed) - 10}" if len(files_changed) > 10 else ""
        lines.append(f"- files_changed: {len(files_changed)} — {listed}{more}")
    tests_run = data.get("tests_run")
    if tests_run:
        lines.append("- tests_run:")
        for item in tests_run:
            lines.append(f"  - `{item['cmd']}` → {item['result']}")
    gates = data.get("gates")
    if gates:
        lines.append(f"- gates: {', '.join(gates)}")
    notes = data.get("notes")
    if notes:
        lines.append(f"- notes: {notes}")
    return lines


def collect_done_facts(
    outbox: Path, todo_id: str, logs_dir: Path
) -> tuple[str, list[str]]:
    """Read + validate DONE-{todo_id}.json for the handoff.

    Returns ``(fact_suffix, machine_facts_lines)``: ``fact_suffix`` is
    ``", DONE-json=present"`` or ``", DONE-json=absent"`` for the handoff
    Run-facts line; ``machine_facts_lines`` are the rendered section lines
    (empty when the file is absent, empty, or broken). A broken or
    schema-violating file logs a warning and never raises.
    """
    fact = ", DONE-json=absent"
    path = find_signal_file(outbox, "DONE", todo_id, ".json")
    if path is None:
        return fact, []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _log(logs_dir, f"U3: {path.name} unreadable ({e}) — handoff skips machine facts")
        return fact, []
    if not text.strip():
        return fact, []
    data = parse_done_json(text)
    if data is None:
        _log(
            logs_dir,
            f"U3: {path.name} is broken or violates the DONE.json schema — "
            "handoff continues without the machine-facts section",
        )
        return fact, []
    lines = render_done_json(data)
    return (fact if not lines else ", DONE-json=present"), lines


__all__ = [
    "KNOWN_GATES",
    "parse_todo_contract",
    "parse_done_json",
    "render_done_json",
    "collect_done_facts",
]
