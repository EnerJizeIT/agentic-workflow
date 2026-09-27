"""ORCH M1.4 (TODO-0131): компактный промпт verify не противоречит сам себе.

До: ``get_phase_prompt("verify", ...)`` склеивал ``_core.md`` с
``phase-verify.md``. В ``_core.md`` строка
«**YOU do NOT:** ... run tests manually, ...» подмешивалась в КАЖДУЮ фазу,
включая verify, а ``phase-verify.md`` требовал «Run verify commands from
the TODO» — модель получала запрет и требование в одном промпте (повторная
проверка 27.09, 16-supervisor.md п.3).

Покрывают:
- test_verify_prompt_has_no_manual_tests_ban — в собранном промпте verify
  нет запрещающей строки про ручной запуск проверок, а требование
  запустить verify-команды из TODO остаётся
- test_all_phase_prompts_no_full_suite_requirement — промпт КАЖДОЙ фазы
  собирается реальным механизмом, и ни одна фаза не требует от
  супервизора запускать полный сьют (это работа воркера)
"""
from __future__ import annotations

import re

from awf.phase import ALL_PHASES, get_phase_prompt

# Запрещающая формулировка внутри строки
PROHIBITION = re.compile(
    r"\b(do\s+not|don'?t|must\s+not|never|forbidd\w*|prohibit\w*)\b",
    re.IGNORECASE,
)
# «run ... manually / by hand» в пределах одной строки
MANUAL_RUN = re.compile(
    r"\b(run|runs|running)\b[^\n]{0,40}\b(manually|by hand)\b",
    re.IGNORECASE,
)
# Требование запустить ПОЛНЫЙ сьют (работа воркера, не супервизора)
FULL_SUITE = re.compile(
    r"\b(run|runs|execute|launch)\b[^\n]{0,60}\bfull (?:test )?suite\b",
    re.IGNORECASE,
)


def _manual_run_ban_lines(prompt: str) -> list[str]:
    """Строки, где запрет и ручной запуск проверок стоят вместе."""
    return [
        line.strip()
        for line in prompt.splitlines()
        if PROHIBITION.search(line) and MANUAL_RUN.search(line)
    ]


def test_verify_prompt_has_no_manual_tests_ban(tmp_path):
    """Промпт verify: запрет ручных проверок убран, требование — на месте."""
    prompt = get_phase_prompt("verify", tmp_path)
    banned = _manual_run_ban_lines(prompt)
    assert not banned, f"verify-промпт всё ещё запрещает ручные проверки: {banned}"
    assert re.search(r"run verify commands", prompt, re.IGNORECASE), (
        "verify-промпт потерял требование запустить verify-команды из TODO"
    )


def test_all_phase_prompts_no_full_suite_requirement(tmp_path):
    """Все фазы собираются; полный сьют нигде не требуется от супервизора."""
    for phase in ALL_PHASES:
        prompt = get_phase_prompt(phase, tmp_path)
        assert prompt.strip(), f"пустой промпт для фазы {phase}"
        match = FULL_SUITE.search(prompt)
        assert not match, (
            f"фаза {phase} требует запускать полный сьют: {match.group(0)}"
        )
