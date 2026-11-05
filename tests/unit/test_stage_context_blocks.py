"""T9/T11/T19/T20/T22/T28/T29/T40/T54: what each block says and where it sits.

The block renderers are tested against a real artifact tree in ``tmp_path``:
TODO with ``AC#`` and ``verify:``, a ``## REVIEW-фикс`` report, four
handoffs, one doctrine file.
"""
from __future__ import annotations

from pathlib import Path

from awf.stage_context import build_stage_context

TODO_ID = "TODO-0401"

TODO_TEXT = """\
---
files: ["awf/a.py"]
verify: ["pytest -q tests/unit/test_a.py", "ruff check ."]
gates: ["failed"]
status: done
---
# TODO — sample

## Приёмка (факты)

- AC1: `f()` возвращает 1.
- AC41: значение вне семейств — `CTX-TYPE-MISSING`.

## Границы

- `awf/other.py` не трогать.
"""

REVIEW_TEXT = """\
# REVIEW

## REVIEW-фикс

R-1: MINOR — переименовать переменную.
закрыто: 1 (R-1)
Тело R-1: подробный разбор, который не должен пересказываться.

R-2: MAJOR — поправить границу allowlist.

BLOCKER R-3: стадия не собирает текст.
"""

STAGES = {
    "plan": ("plan", "supervisor"),
    "agent-implementer": ("execute", "agent-implementer"),
    "agent-qa-review": ("reviewer", "agent-qa-review"),
    "checklist-close": ("reviewer/checklist", "agent-implementer"),
    "verify": ("supervisor", "supervisor"),
    "spec-design": ("spec", "agent-system-analyst"),
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    agentic = root / ".agentic"
    for name in ("inbox", "outbox", "context", "logs", "handoff", "roles", "phases", "doctrine"):
        (agentic / name).mkdir(parents=True)
    _write(
        agentic / "config.yaml",
        "project:\n  name: T\nstage_types:\n  plan: plan\n  agent-implementer: execute\n"
        "  agent-qa-review: reviewer\n  checklist-close: reviewer/checklist\n"
        "  verify: supervisor\n  spec-design: spec\n",
    )
    _write(agentic / "inbox" / f"{TODO_ID}.md", TODO_TEXT)
    _write(
        agentic / "context" / f"PIPELINE-{TODO_ID}.yaml",
        "name: default\nstages:\n- name: agent-implementer\n  role: agent-implementer\n  task: 'Implement.'\n",
    )
    _write(agentic / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md", REVIEW_TEXT)
    _write(
        agentic / "context" / f"GATES-{TODO_ID}.md",
        "## Verdict: **ok**\n\n```json\n{\"verdict\": \"ok\", \"sections\": {\"tests\": \"pass\"}}\n```\n",
    )
    _write(agentic / "context" / f"BASELINE-{TODO_ID}.sha", "deadbeef\n")
    _write(agentic / "doctrine" / "01-a.md", "# Доктрина\n\nДоктрина — один блок.\n")
    _write(agentic / "doctrine" / "02-b.md", "# Вторая доктрина\n\nЕщё текст.\n")
    for index, size in enumerate((400, 900, 3000, 2000)):
        _write(agentic / "handoff" / f"stage{index}-{TODO_ID}.md", f"# handoff {index}\n" + "h" * size)
    _write(agentic / "roles" / "agent-implementer.md", "# Роль\n\n## Instructions\n" + "роль " * 500)
    _write(agentic / "roles" / "agent-qa-review.md", "# Роль\n\n## Инструкции\nИщи баги.\n")
    _write(agentic / "roles" / "supervisor.md", "# Роль\n\n## Роль\nВеди пайплайн.\n")
    _write(agentic / "roles" / "agent-system-analyst.md", "# Роль\n\n## Инструкции\nПиши требования.\n")
    _write(agentic / "phases" / "plan.md", "# Фазы\n\n1. Одна фаза.\n")
    _write(agentic / "outbox" / f"DONE-{TODO_ID}.md", "# DONE — sample\n")
    _write(agentic / "outbox" / f"DONE-{TODO_ID}.json", '{"status": "blocked", "files_changed": ["awf/a.py"]}')
    return root


def _build(root: Path, stage: str):
    _family, role = STAGES[stage]
    return build_stage_context(root, TODO_ID, stage, role)


def test_T9_output_contract_in_every_type(tmp_path: Path) -> None:
    root = project(tmp_path)
    for stage in STAGES:
        result = _build(root, stage)
        text = result.text
        for phrase in ("DONE", "BLOCKED", "REVIEW", "молчание: провал"):
            assert phrase in text, (stage, phrase)
        assert result.blocks[-1].id == "OUTPUT"


def test_T19_block_positions(tmp_path: Path) -> None:
    root = project(tmp_path)
    for stage in STAGES:
        result = _build(root, stage)
        text = result.text
        first = result.blocks[0].id
        assert first == "STAGE"
        assert text.index("## Стадия") == 0
        if stage == "plan":
            second = result.blocks[1].id
            assert second == "PHASES"
        elif stage != "checklist-close":
            assert result.blocks[1].id in {"UNIT", "STATE", "CHECK"}
        assert text.rindex("## Контракт вывода") > text.index("## Правила")


def test_T20_review_goes_before_the_role(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    positions = result.positions
    assert positions["REVIEW"] < positions["ROLE"]
    assert positions["REVIEW"] < positions["DOCTRINE"]
    assert result.text.index("R-2") < result.text.index("## Доктрина проекта")


def test_T22_ac_numbers_and_verify_commands_are_literal(tmp_path: Path) -> None:
    root = project(tmp_path)
    text = _build(root, "agent-implementer").text
    assert "AC1" in text
    assert "AC41" in text
    assert "pytest -q tests/unit/test_a.py" in text
    assert "ruff check ." in text


def test_T29_one_handoff_is_text_the_rest_are_links(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    prev = next(block for block in result.blocks if block.id == "PREV")
    links = next(block for block in result.blocks if block.id == "PREVLINK")
    assert "stage3-" in prev.source
    assert prev.text.startswith("# handoff 3")
    for index in range(3):
        assert f"stage{index}-{TODO_ID}.md" in links.text
    assert links.source.endswith("handoff")


def test_T40_closed_findings_are_counted_not_retold(tmp_path: Path) -> None:
    root = project(tmp_path)
    assert not (root / ".agentic" / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.ready").exists()
    text = _build(root, "agent-implementer").text
    assert "R-2" in text
    assert "закрыто: 1 (R-1)" in text
    assert "подробный разбор" not in text


def test_T28_blocker_survives_a_review_cut(tmp_path: Path) -> None:
    root = project(tmp_path)
    review = root / ".agentic" / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md"
    general = "\n\n".join(f"R-{10 + i}: MINOR — замечание {i}. " + "x" * 200 for i in range(20))
    _write(review, f"# REVIEW\n\n## REVIEW-фикс\n\n{general}\n\nBLOCKER R-99: критично.\n")
    result = _build(root, "agent-implementer")
    review_block = next(block for block in result.blocks if block.id == "REVIEW")
    assert "BLOCKER R-99" in review_block.text + review_block.tail
    assert "ещё " in result.text
    assert "R-99" in result.text


def test_T54_doctrine_is_one_block(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    doctrine = [block for block in result.blocks if block.id == "DOCTRINE"]
    assert len(doctrine) == 1
    assert result.text.count("## Доктрина проекта") == 1
    assert "01-a.md" in doctrine[0].text


def test_T11_conflicting_owners_are_reported(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    assert any("CTX-CONFLICT" in line for line in result.conflicts)
    assert any("unit.status" in line for line in result.conflicts)
    assert any("gates.verdict" in line for line in result.conflicts)


def test_missing_block_line_names_reason_and_path(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    line = next(line for line in result.text.splitlines() if line.startswith("нет данных:"))
    assert " — " in line


def test_review_findings_are_read_from_the_todo_rounds(tmp_path: Path) -> None:
    """AC12: findings live in the TODO's ``## REVIEW-фикс`` rounds.

    ``hygiene.review_append`` appends the round to the TODO; the reviewer's
    outbox report in this tree carries no such heading. A reader that only
    looked at the report said «секции находок нет» while the TODO held it.
    """
    root = project(tmp_path)
    todo = root / ".agentic" / "inbox" / f"{TODO_ID}.md"
    todo.write_text(
        TODO_TEXT
        + "\n\n## REVIEW-фикс (супервизор, виток 1)\n\nRR1 (MAJOR) — первая находка.\n"
        + "\n\n## REVIEW-фикс (супервизор, виток 2)\n\nR-2: MAJOR — новая находка.\n",
        encoding="utf-8",
    )
    (root / ".agentic" / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md").write_text(
        f"# REVIEW-REJECTED — {TODO_ID}\n\nСтатус: FAIL.\n", encoding="utf-8"
    )
    result = _build(root, "agent-implementer")
    assert "REVIEW" not in result.missing
    assert "новая находка" in result.text
    assert "закрыто: 1 (RR1)" in result.text
    assert "первая находка" not in result.text


def test_closed_line_names_the_findings_own_id(tmp_path: Path) -> None:
    """The ID comes from the finding's head, not from an ``R-N`` in its body."""
    root = project(tmp_path)
    todo = root / ".agentic" / "inbox" / f"{TODO_ID}.md"
    todo.write_text(
        TODO_TEXT
        + "\n\n## REVIEW-фикс (супервизор, виток 1)\n\n"
        + "RR1 (MAJOR) — закрыто, см. R-4.\n\nR-9: MINOR — открыто.\n",
        encoding="utf-8",
    )
    (root / ".agentic" / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md").write_text(
        f"# REVIEW-REJECTED — {TODO_ID}\n", encoding="utf-8"
    )
    result = _build(root, "agent-implementer")
    assert "закрыто: 1 (RR1)" in result.text
    assert "R-9" in result.text
