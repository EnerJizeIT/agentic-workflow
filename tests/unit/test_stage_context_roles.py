"""T30–T33: the role core — selection by ``##``, coverage, no second copy.

The role files are built in ``tmp_path`` with known headers, so the test is
reproducible on a clean clone: no runtime role directory is read. Each
fixture keeps at least three of its own phrases in the core, the header stays
under 500, a section outside the marker table becomes a table-of-contents
line, and the coverage is ``floor(taken × 100 / total)``.
"""
from __future__ import annotations

from pathlib import Path

from awf.stage_context_roles import HEADER_LIMIT, ROLE_LOW_PCT, marker_priority, select_role_core

#: T30: the role population and three placeholder phrases each. The names
#: mirror the real role set (so a role that stops being selectable is still
#: visible here), the phrases are arbitrary text: the assertion is that the
#: priority headers survive the selection, not that a shipped role says this.
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


def _role_text(name: str, phrases: tuple[str, ...]) -> str:
    """A role fixture with known priority headers and its three phrases."""
    first, second, third = phrases
    return (
        f"# ROLE: {name}\n\n"
        f"## Instructions\n{first} — основное правило роли.\n\n"
        f"## Handoff contract\n{second} — что роль отдаёт дальше.\n\n"
        f"## Запреты\n{third} — чего делать нельзя.\n"
    )


def _roles(tmp_path: Path) -> Path:
    root = tmp_path / "roles"
    root.mkdir(exist_ok=True)
    for name, phrases in PHRASES.items():
        (root / name).write_text(_role_text(name, phrases), encoding="utf-8")
    return root


def test_T30_every_role_keeps_its_phrases(tmp_path: Path) -> None:
    roles = _roles(tmp_path)
    for name, phrases in PHRASES.items():
        path = roles / name
        assert path.is_file(), name
        selection = select_role_core(path.read_text(encoding="utf-8"), "execute", 9000, source=str(path))
        lowered = selection.text.lower()
        assert sum(1 for phrase in phrases if phrase.lower() in lowered) >= 3, name
        assert selection.total > 0


def test_T32_header_and_core_respect_their_limits(tmp_path: Path) -> None:
    path = _roles(tmp_path) / "agent-system-analyst.md"
    text = path.read_text(encoding="utf-8")
    header = text[: text.index("## ")]
    limit = 120  # header + "Instructions" fit, the other two go to the TOC
    selection = select_role_core(text, "spec", limit, source=str(path))
    assert selection.text.startswith(header[:HEADER_LIMIT])
    assert selection.taken <= limit
    assert "Не вошло в ядро" in selection.text
    assert not list(path.parent.glob("*.core.md"))

    # A section that only partly fits fills the budget and names its tail.
    big = "# Роль\n\n## Instructions\n" + "a" * 900 + "\n"
    partial = select_role_core(big, "execute", 500, source="role.md")
    assert "… ещё" in partial.text
    assert partial.taken <= 500

    long_header = "# " + "h" * 900 + "\n\n## Instructions\nделай\n\n## Посторонний раздел\nтекст\n"
    cut = select_role_core(long_header, "execute", 200, source="role.md")
    assert "§Посторонний раздел" in cut.text
    assert len(cut.text.split("\n", 1)[0]) <= HEADER_LIMIT


def test_priority_order_wins_the_budget_over_file_order() -> None:
    """A low-priority section listed first must not take the budget.

    ``Запреты`` (priority 4) stands before ``Instructions`` (priority 1) in
    the file. The budget fits one section: the priority table, not the file
    order, decides which one survives.
    """
    text = "# Роль\n\n## Запреты\nнельзя\n\n## Instructions\n" + "a" * 400 + "\n"
    selection = select_role_core(text, "execute", 208, source="role.md")
    assert "a" * 150 in selection.text
    assert "нельзя" not in selection.text
    assert selection.taken == 208  # the budget is spent on the priority section


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


def test_role_without_known_headers_is_only_a_toc_line(tmp_path: Path) -> None:
    path = tmp_path / "no-headers.md"
    path.write_text("# Роль\n\n## Посторонний раздел\n" + "x" * 1000, encoding="utf-8")
    selection = select_role_core(path.read_text(encoding="utf-8"), "execute", 5000, source=str(path))
    assert "§Посторонний раздел" in selection.text
    assert "x" * 1000 not in selection.text


def test_low_coverage_role_warns_below_the_threshold(tmp_path: Path) -> None:
    """CTX-ROLE-LOW: a core that keeps a third of the role is flagged."""
    low = tmp_path / "low.md"
    low.write_text("# Роль\n\n## Instructions\n" + "a" * 1000, encoding="utf-8")
    selection = select_role_core(low.read_text(encoding="utf-8"), "execute", 300, source=str(low))
    assert selection.low
    assert selection.pct < ROLE_LOW_PCT


def test_empty_role_file_gives_an_empty_selection(tmp_path: Path) -> None:
    empty = tmp_path / "empty.md"
    empty.write_text("", encoding="utf-8")
    nothing = select_role_core(empty.read_text(encoding="utf-8"), "execute", 1000, source=str(empty))
    assert nothing.text == ""
    assert nothing.taken == 0


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
