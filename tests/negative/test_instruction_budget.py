"""Бюджет инструкций: `scripts/instruction-gates.sh` держит лимит.

Инструкции читает каждая сессия, поэтому лимит — храповик: текст растёт
только вместе с удалением старого. Тест гоняет тот же шаг ворот
`instructions`, что и `scripts/run-all.sh`, и падает, если бюджет переполнен
или проверка сломана. Отдельно проверяет, что ворота умеют краснеть, что
потолок не поднят и что мерится то же множество файлов.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GATES = REPO / "scripts" / "instruction-gates.sh"
CONF = REPO / "scripts" / "instruction-gates.conf"
BUDGET_RE = re.compile(r"бюджет: (\d+) из (\d+) слов")
FILES_RE = re.compile(r"бюджет: \d+ из \d+ слов, файлов: (\d+)")
#: Потолок храповика (TODO-0228): поднять лимит — значит стереть проверку.
LIMIT_CEILING = 950


def _gates(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GATES), *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _conf(name: str) -> str:
    """Значение переменной конфига ворот, как его увидит скрипт."""
    text = CONF.read_text(encoding="utf-8")
    match = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
    assert match, f"в instruction-gates.conf нет {name}"
    return match.group(1).strip().strip('"').strip("'")


def test_instruction_gates_green():
    proc = _gates()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✗" not in proc.stdout


def test_instruction_budget_within_limit():
    proc = _gates()
    match = BUDGET_RE.search(proc.stdout)
    assert match, proc.stdout + proc.stderr
    used, limit = (int(group) for group in match.groups())
    assert used <= limit, f"бюджет: {used} слов при лимите {limit}"


def test_gate_goes_red_when_budget_is_exceeded(tmp_path: Path):
    """Ворота умеют краснеть: иначе зелёный цвет ничего не доказывает."""
    conf = tmp_path / "over-budget.conf"
    conf.write_text(
        'INSTR_FILES="AGENTS.md"\nINSTR_WORD_LIMIT=1\n'
        'HASH_FILES=""\nDOCTRINE_DIR=""\n',
        encoding="utf-8",
    )
    proc = _gates(str(conf))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "✗ бюджет" in proc.stdout, proc.stdout
    assert "при лимите 1" in proc.stdout, proc.stdout


def test_word_limit_is_a_ratchet():
    """Лимит можно опускать, но не поднимать: рост текста требует удаления."""
    limit = int(_conf("INSTR_WORD_LIMIT"))
    assert limit <= LIMIT_CEILING, (
        f"INSTR_WORD_LIMIT={limit} выше потолка {LIMIT_CEILING}: "
        "лимит — храповик, сокращай текст, а не потолок"
    )


def test_measured_files_are_agents_and_contracts():
    """Мерится то же множество: ядро инструкций и все контракты."""
    patterns = _conf("INSTR_FILES").split()
    assert "AGENTS.md" in patterns, patterns
    assert "docs/contracts/*.md" in patterns, patterns
    missing = [pattern for pattern in patterns if not list(REPO.glob(pattern))]
    assert not missing, f"INSTR_FILES указывает в пустоту: {missing}"


def test_budget_reports_every_measured_file():
    """Счётчик файлов совпадает с реальным: пропажа файла не проходит молча."""
    patterns = _conf("INSTR_FILES").split()
    expected = len({path for pattern in patterns for path in REPO.glob(pattern)})
    match = FILES_RE.search(_gates().stdout)
    assert match, "в выводе ворот нет бюджета с числом файлов"
    assert int(match.group(1)) == expected, (
        f"ворота мерят {match.group(1)} файлов, а по INSTR_FILES их {expected}"
    )
