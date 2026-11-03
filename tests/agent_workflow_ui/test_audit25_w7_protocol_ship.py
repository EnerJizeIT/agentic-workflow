"""ORCH M1.4 (TODO-0131): шиппящийся шаблон супервизора.

До: копия, шиппящаяся с пакетом (``awf/templates/roles/supervisor/**``),
и корневое зеркало не имели механической проверки синхронности:
расхождение между двумя копиями замечалось вручную (противоречие «do not
run tests manually» в ``_core.md`` против требования запустить проверки на
verify — повторная проверка 27.09, 16-supervisor.md п.3).

TODO-0230 (Y1): корневое зеркало и симлинк удалены — источник один,
пакетный. Тест синхронности копий больше не нужен.

Покрывает:
- test_shipped_supervisor_templates_present — пакетный набор на месте
- test_full_supervisor_does_not_contradict_compact_verify — полный
  supervisor.md и компактная фаза verify говорят ОДНО И ТО ЖЕ (A42:
  прогоны уже сделал пак, повторять их не надо), а ``_core.md`` не несёт
  исключения для verify (иначе промпты противоречат друг другу)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = REPO_ROOT / "awf" / "templates"

# Те же детекторы, что в
# tests/negative/test_verify_prompt_no_contradiction.py
PROHIBITION = re.compile(
    r"\b(do\s+not|don'?t|must\s+not|never|forbidd\w*|prohibit\w*)\b",
    re.IGNORECASE,
)
MANUAL_RUN = re.compile(
    r"\b(run|runs|running)\b[^\n]{0,40}\b(manually|by hand)\b",
    re.IGNORECASE,
)


def _dir_files(base: Path) -> dict[str, str]:
    assert base.is_dir(), f"no template dir: {base}"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(base.glob("*.md"))}


def test_shipped_supervisor_templates_present():
    shipped = _dir_files(TEMPLATES / "roles" / "supervisor")
    assert "phase-verify.md" in shipped
    assert (TEMPLATES / "roles" / "supervisor.md").is_file()
    assert not (REPO_ROOT / "templates").exists(), (
        "repo-root templates/ copy is back — the package is the only source"
    )


# A42: обе копии инструкций обязаны говорить «прогоны уже сделал пак».
# До A42 тест требовал обратного — «run verification commands from config
# independently» в полном supervisor.md, — и после смены контракта сам стал
# источником противоречия: промпт запрещал прогон, а гейт требовал строку.
STALE_VERIFY_INSTRUCTIONS = (
    "run verification commands from config independently",
    "Run ALL handoff files".lower(),
    "Read ALL handoff files".lower(),
)


def test_full_supervisor_does_not_contradict_compact_verify():
    """Полный и компактный промпт согласованы на стадии verify (A42)."""
    full = (TEMPLATES / "roles" / "supervisor.md").read_text(encoding="utf-8")
    phase = (TEMPLATES / "roles" / "supervisor" / "phase-verify.md").read_text(
        encoding="utf-8"
    )
    for label, text in (("supervisor.md", full), ("phase-verify.md", phase)):
        assert re.search(r"do not re-run", text, re.IGNORECASE), (
            f"{label} потерял запрет повторного прогона — стадия снова "
            "поедет перепроверять то, что уже измерил пак"
        )
        assert "GATES-" in text, f"{label} не называет вердикт пака"
        stale = [p for p in STALE_VERIFY_INSTRUCTIONS if p in text.lower()]
        assert not stale, f"{label} всё ещё требует прежнего поведения: {stale}"

    core = (TEMPLATES / "roles" / "supervisor" / "_core.md").read_text(encoding="utf-8")
    banned = [
        line.strip()
        for line in core.splitlines()
        if PROHIBITION.search(line) and MANUAL_RUN.search(line)
    ]
    assert not banned, (
        "компактный _core.md противоречит полному supervisor.md: " f"{banned}"
    )
