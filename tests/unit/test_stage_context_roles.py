"""T30–T33: the role core — selection by ``##``, coverage, no second copy.

The real files in ``.agentic/roles/`` are READ (never written): each keeps at
least three of its own phrases in the core, the header stays under 500, a
section outside the marker table becomes a table-of-contents line, and the
coverage is ``floor(taken × 100 / total)``.
"""
from __future__ import annotations

from pathlib import Path

from awf.stage_context_roles import HEADER_LIMIT, ROLE_LOW_PCT, marker_priority, select_role_core

REPO = Path(__file__).resolve().parents[2]
ROLES = REPO / ".agentic" / "roles"

#: T30: three phrases per role, taken from the role files themselves.
PHRASES = {
    "supervisor.md": ("supervisor", "pipeline", "prohibitions"),
    "agent-implementer.md": ("Реализуй", "тест", "commit"),
    "agent-qa-review.md": ("проверк", "тест", "выход"),
    "agent-project-auditor.md": ("аудит", "проект", "риск"),
    "agent-architect-review.md": ("maintainability", "coherence", "invariant"),
    "agent-system-analyst.md": ("требован", "сценари", "готовност"),
    "agent-architector.md": ("контракт", "требован", "проектирован"),
    "agent-requirements-reviewer.md": ("требован", "проверк", "пакет"),
    "agent-refactoring-specialist.md": ("refactoring", "scope control", "complexity"),
}


def test_T30_every_role_keeps_its_phrases() -> None:
    for name, phrases in PHRASES.items():
        path = ROLES / name
        assert path.is_file(), name
        selection = select_role_core(path.read_text(encoding="utf-8"), "execute", 9000, source=str(path))
        lowered = selection.text.lower()
        assert sum(1 for phrase in phrases if phrase.lower() in lowered) >= 3, name
        assert selection.total > 0


def test_T32_header_and_core_respect_their_limits() -> None:
    path = ROLES / "agent-system-analyst.md"
    text = path.read_text(encoding="utf-8")
    selection = select_role_core(text, "spec", 1000, source=str(path))
    assert selection.text[:HEADER_LIMIT] == text[:HEADER_LIMIT]
    assert selection.taken <= 1000
    assert "Не вошло в ядро" in selection.text
    assert not list(ROLES.glob("*.core.md"))

    long_header = "# " + "h" * 900 + "\n\n## Instructions\nделай\n\n## Посторонний раздел\nтекст\n"
    cut = select_role_core(long_header, "execute", 200, source="role.md")
    assert "§Посторонний раздел" in cut.text
    assert len(cut.text.split("\n", 1)[0]) <= HEADER_LIMIT


def test_T33_coverage_is_floor_and_warns_below_70() -> None:
    text = "# Роль\n\n## Instructions\n" + "a" * 1000
    full = select_role_core(text, "execute", 5000)
    assert full.pct == 100
    assert not full.low

    at_70 = select_role_core(text, "execute", -(-full.total * 70 // 100))
    assert at_70.pct == 70
    assert not at_70.low

    at_69 = select_role_core(text, "execute", -(-full.total * 69 // 100))
    assert at_69.pct == 69
    assert at_69.low
    assert at_69.pct < ROLE_LOW_PCT


def test_T33_695_of_1000_reads_as_69() -> None:
    header = "# Роль\n"
    body = "## Instructions\n" + "a" * (1000 - len(header) - len("## Instructions\n"))
    selection = select_role_core(header + body, "execute", 695)
    assert selection.pct == (selection.taken * 100) // selection.total
    assert selection.pct == 69
    assert selection.low is (selection.pct < ROLE_LOW_PCT)


def test_markers_follow_the_priority_table() -> None:
    assert marker_priority("Instructions") == 1
    assert marker_priority("Handoff contract") == 2
    assert marker_priority("Режим") == 3
    assert marker_priority("Запреты") == 4
    assert marker_priority("Политика тестов (added by awf add_role)") == 5
    assert marker_priority("Реализовано: <задача>") is None


def test_checklist_row_has_no_role_core() -> None:
    selection = select_role_core("# Роль\n\n## Instructions\nтекст", "reviewer/checklist", 400)
    assert selection.text == ""
    assert selection.pct == 100
