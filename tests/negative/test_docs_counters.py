"""TODO-0120 (wave 8.2): the tools counter in live docs matches the
current total.

1.4.0 shipped 47 tools (5 UI + 42 workflow); ORCH M3.4 (TODO-0138) added
``awf_run_revise`` — 48 tools (5 UI + 43 workflow). The docs carried the
stale totals (47 / 45 / 37 = 5 UI + 32 workflow) in several places. This
guard pins every live doc that claims a tools counter:

  * the current total ``48`` is present in a counting context — a line
    that talks about tools / the UI+workflow split;
  * no stale total (47 / 45 / 37 / 32) appears in a counting context.

A bare number on a non-counting line (a line number, a year, ...) is not
flagged — only lines that count tools.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Module-level, like test_audit25_r06_registry.py: at collection time there
# is no active test, so no monkeypatched subprocess.Popen — the mcp import
# chain (which annotates ``subprocess.Popen[bytes]``) must not run inside a
# test body, where tests/conftest.py patches Popen to a plain function.
pytest.importorskip("mcp.server.fastmcp")
from agent_workflow_ui.tools import registry as _registry  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]

DOCS = [
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.ru.md",
    REPO_ROOT / "docs" / "vision.md",
    REPO_ROOT / "agent_workflow_ui" / "README.md",
]

_CURRENT_RE = re.compile(r"\b48\b")
_STALE_RE = re.compile(r"\b(?:47|45|37|32)\b")

# A line counts tools when it mentions "tool(s)" ...
_TOOL_LINE = re.compile(r"\btools?\b", re.I)
# ... or states the UI/workflow split ("5 UI + 42 workflow", "Workflow (42)",
# "UI (5)").
_SPLIT_LINE = re.compile(
    r"\b\d+\s*UI\b|\bUI\s*\(\d+\)|\b\d+\s*workflow\b|\bworkflow\s*\(\d+\)", re.I
)


def _counting_lines(doc: Path) -> list[str]:
    """Lines of ``doc`` that count tools (the counter's context)."""
    return [
        line
        for line in doc.read_text(encoding="utf-8").splitlines()
        if _TOOL_LINE.search(line) or _SPLIT_LINE.search(line)
    ]


def test_docs_exist():
    missing = [str(p.relative_to(REPO_ROOT)) for p in DOCS if not p.is_file()]
    assert not missing, f"missing docs: {missing}"


def test_current_counter_present():
    """Each doc carries the current total (48) in a counting line."""
    bad = []
    for doc in DOCS:
        lines = _counting_lines(doc)
        assert lines, f"{doc.relative_to(REPO_ROOT)}: no tools-counting line at all"
        if not any(_CURRENT_RE.search(line) for line in lines):
            bad.append(str(doc.relative_to(REPO_ROOT)))
    assert not bad, f"docs without the current '48' counter: {bad}"


def test_no_stale_counter_in_counting_context():
    """47/45/37/32 must not survive in a line that counts tools."""
    stale = []
    for doc in DOCS:
        for line in _counting_lines(doc):
            if _STALE_RE.search(line):
                stale.append(f"{doc.relative_to(REPO_ROOT)}: {line.strip()}")
    assert not stale, (
        "stale tools counter (47/45/37/32) in a counting line:\n" + "\n".join(stale)
    )


def test_ui_workflow_split_matches_registry():
    """Explicit splits ('5 UI + 42 workflow', 'Workflow (42)') track the
    live MCP registry — the single source of truth (R-06)."""
    names = [t.name for t in _registry.TOOLS]
    ui = [n for n in names if not n.startswith("awf_")]
    awf = [n for n in names if n.startswith("awf_")]
    for doc in DOCS:
        for line in _counting_lines(doc):
            m = re.search(r"\(\s*(\d+)\s*UI\s*\+\s*(\d+)\s*workflow\s*\)", line)
            if m:
                assert (int(m.group(1)), int(m.group(2))) == (len(ui), len(awf)), (
                    f"{doc.relative_to(REPO_ROOT)}: split {m.group(0)} disagrees "
                    f"with the registry ({len(ui)} UI + {len(awf)} workflow)"
                )
            m = re.search(r"\bUI\s*\((\d+)\)", line, re.I)
            if m:
                assert int(m.group(1)) == len(ui), (
                    f"{doc.relative_to(REPO_ROOT)}: 'UI ({m.group(1)})' but the "
                    f"registry has {len(ui)} UI tools"
                )
            m = re.search(r"\bworkflow\s*\((\d+)\)", line, re.I)
            if m:
                assert int(m.group(1)) == len(awf), (
                    f"{doc.relative_to(REPO_ROOT)}: 'workflow ({m.group(1)})' but "
                    f"the registry has {len(awf)} workflow tools"
                )


def _missing_awf_modules(table: str) -> list[str]:
    """awf/.py and awf/api/*.py files absent from the design.md table.

    Matched by exact backtick token (so ``pipeline.py`` is not satisfied by
    the ``api/pipeline.py`` row). Grouped tokens count (``cmd_*`` covers
    every ``cmd_*.py``); ``__init__.py`` is excluded.
    """
    tokens = set(re.findall(r"`([^`]+)`", table))
    cmd_glob = any(t.startswith("cmd_") and "*" in t for t in tokens)
    missing = []
    for rel, label_prefix in (("awf", ""), ("awf/api", "api/")):
        for f in sorted((REPO_ROOT / rel).glob("*.py")):
            if f.name == "__init__.py":
                continue
            if f.name.startswith("cmd_") and cmd_glob:
                continue
            if f"{label_prefix}{f.name}" not in tokens:
                missing.append(f"{rel}/{f.name}")
    return missing


def test_design_component_table_covers_awf_modules():
    """The awf component table in docs/design.md is current.

    The unit contract (invariant 1) requires the component list to be
    cross-checked against ``awf/*.py`` and ``awf/api/*.py``. This
    guard catches modules that appear after the table was written —
    including ones that back public tools (``api/pipelines.py``,
    ``api/planning.py``, ``api/roles.py``) and the core stage loader
    (``awf/pipeline.py``).
    """
    design = (REPO_ROOT / "docs" / "design.md").read_text(encoding="utf-8")
    table = design[
        design.index("### awf core") : design.index("### agent_workflow_ui plugin")
    ]
    missing = _missing_awf_modules(table)
    assert not missing, f"design.md component table misses: {missing}"
