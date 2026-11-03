"""Verify-промпт не спорит сам с собой (ORCH M1.4 → A42).

История: ``get_phase_prompt("verify", ...)`` склеивает ``_core.md`` с
``phase-verify.md``. В ``_core.md`` стоял запрет ручных проверок, а
``phase-verify.md`` требовал «Run verify commands from the TODO» — модель
получала запрет и требование в одном промпте (повторная проверка 27.09).

A42 перевернул правило: прогоны уже сделал verify-пак, супервизор решает
по его фактам. Значит фаза verify обязана (а) не требовать прогонов,
(б) не отправлять читать handoff-историю, (в) называть вердикт пака.
Тест держит все три пункта, чтобы шаблон не откатился к «запусти сам».

Второй тест — прежний: ни одна фаза не требует полного сьюта от
супервизора.
"""
from __future__ import annotations

import re
from pathlib import Path

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


def _manual_run_contradictions(prompt: str) -> list[str]:
    """Строки, где запрет и требование ручного прогона стоят вместе."""
    return [
        line.strip()
        for line in prompt.splitlines()
        if PROHIBITION.search(line) and MANUAL_RUN.search(line)
    ]


def test_verify_phase_prompt_decides_from_pack_facts(tmp_path):
    """A42: факты пака вместо «прочитай handoff-ы и запусти проверки»."""
    prompt = get_phase_prompt("verify", tmp_path)
    flat = re.sub(r"\s+", " ", prompt)

    assert "Read ALL handoffs" not in flat
    assert "Run verify commands from the TODO" not in flat
    assert "GATES-" in prompt, "фаза verify должна называть вердикт пака"
    assert "do not re-run" in flat.lower(), (
        "фаза verify должна запрещать повторный прогон проверок"
    )
    assert not _manual_run_contradictions(prompt)


def test_all_phase_prompts_no_full_suite_requirement(tmp_path):
    """Все фазы собираются; полный сьют нигде не требуется от супервизора."""
    for phase in ALL_PHASES:
        prompt = get_phase_prompt(phase, tmp_path)
        assert prompt.strip(), f"пустой промпт для фазы {phase}"
        match = FULL_SUITE.search(prompt)
        assert not match, (
            f"фаза {phase} требует запускать полный сьют: {match.group(0)}"
        )


# A42: the role file is NOT the base prompt — it is attached to the verify
# subprocess as ``--file`` (run_supervisor_via_subprocess). Words there weigh
# exactly as much as words in the prompt, so "read the handoffs, run the
# checks" in the role file re-opens the hole the prompt already closed.
ROLE_TEMPLATE = (
    Path(__file__).resolve().parents[2] / "awf" / "templates" / "roles" / "supervisor.md"
)

# Instructions that belong to the pre-A42 verify stage.
STALE_VERIFY_LINES = (
    "Read handoffs.",
    "verify: read handoffs",
    "Run verification commands",
    "Ran full verify independently",
    "Regression verify passed",
    "git diff --stat` shows source file changes",
)


def test_supervisor_role_template_does_not_send_verify_to_handoffs_or_runs():
    """A42: файл роли не должен отправлять verify в handoff-ы и прогоны.

    Регрессия: шаг Verify в шаблоне переписали, а сводку «At verify stage»
    и чек-лист после DONE — нет. Файл роли уходит в субпроцесс как
    ``--file``, поэтому старая строка «Read handoffs. Check git diff» весит
    столько же, сколько промпт, — инвариант держался не на всех поверхностях.
    """
    text = ROLE_TEMPLATE.read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", text)

    stale = [line for line in STALE_VERIFY_LINES if line in flat]
    assert not stale, f"файл роли всё ещё шлёт verify в прошлый век: {stale}"
    assert "pack verdict" in flat.lower() or "pack facts" in flat.lower(), (
        "файл роли должен называть факты пака входом verify"
    )
