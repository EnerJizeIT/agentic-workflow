"""ORCH M1.4 (TODO-0131): шиппящийся шаблон супервизора и корневое зеркало.

До: копия, шиппящаяся с пакетом (``awf/templates/roles/supervisor/**``),
и корневое зеркало (``templates/roles/supervisor/**`` — его читает
собственный догфуд проекта первым) не имели механической проверки
синхронности: расхождение между двумя копиями замечалось вручную
(противоречие «do not run tests manually» в ``_core.md`` против
требования запустить проверки на verify — повторная проверка 27.09,
16-supervisor.md п.3).

Покрывают:
- test_shipped_supervisor_templates_match_root_mirror — файл в файл
  идентичны (включая полный supervisor.md)
- test_full_supervisor_does_not_contradict_compact_verify — полный
  supervisor.md несёт требование независимой verify, а компактный
  ``_core.md`` не несёт запрета ручных проверок (иначе полный и
  компактный промпты противоречат друг другу)
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = REPO_ROOT / "awf" / "templates"
MIRROR = REPO_ROOT / "templates"

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


def test_shipped_supervisor_templates_match_root_mirror():
    """Шиппящаяся копия и корневое зеркало идентичны файл в файл."""
    shipped = _dir_files(TEMPLATES / "roles" / "supervisor")
    mirrored = _dir_files(MIRROR / "roles" / "supervisor")
    assert shipped == mirrored, (
        "shipped/root mirror drift: "
        f"shipped={sorted(shipped)} mirrored={sorted(mirrored)}"
    )
    full_shipped = (TEMPLATES / "roles" / "supervisor.md").read_text(encoding="utf-8")
    full_mirror = (MIRROR / "roles" / "supervisor.md").read_text(encoding="utf-8")
    assert full_shipped == full_mirror, "supervisor.md shipped/root copies differ"


def test_full_supervisor_does_not_contradict_compact_verify():
    """Полный и компактный промпт согласованы на стадии verify."""
    full = (TEMPLATES / "roles" / "supervisor.md").read_text(encoding="utf-8")
    assert re.search(
        r"run verification commands from config independently", full, re.IGNORECASE
    ), "supervisor.md потерял требование независимой verify"
    core = (TEMPLATES / "roles" / "supervisor" / "_core.md").read_text(encoding="utf-8")
    banned = [
        line.strip()
        for line in core.splitlines()
        if PROHIBITION.search(line) and MANUAL_RUN.search(line)
    ]
    assert not banned, (
        "компактный _core.md противоречит полному supervisor.md: " f"{banned}"
    )
