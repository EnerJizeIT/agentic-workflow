"""T1/T12/T14/T17/T23–T25/T31/T36/T42: the builder end to end.

A fixture project lives in ``tmp_path``: a config with ``stage_types``, the
unit's TODO, the artifacts of each type. The builder writes
``STAGE-<todo>-<stage>.md`` and one ``CTX`` line; P0 that does not fit is
``CTX-FAIL`` with no file.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from awf.bounded_prompt import Block, fit_blocks
from awf.stage_context import (
    CAP,
    FAMILIES,
    LIMITS,
    StageContextError,
    build_stage_context,
    load_config,
)

TODO_ID = "TODO-0400"

TODO_TEXT = """\
---
files: ["awf/a.py", "tests/unit/test_a.py"]
verify: ["pytest -q tests/unit/test_a.py"]
gates: ["tests", "lint"]
---
# TODO — sample unit

## Что сделать

1. Добавить функцию `f`.

## Приёмка (факты)

- AC1: `f()` возвращает 1.
- AC2: путь проекта абсолютный.

## Границы

- `awf/other.py` не трогать.

## Чек-лист завершения

- [ ] base-todo: TODO на месте — probe(file_exists [".agentic/inbox/TODO-0400.md"]) expect present
"""

STAGE_TYPES = """\
stage_types:
  plan: plan
  agent-implementer: execute
  agent-qa-review: reviewer
  checklist-close: reviewer/checklist
  verify: supervisor
  spec-design: spec
"""

TYPES = {
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


def project(tmp_path: Path, *, context: bool = True) -> Path:
    """A project with every artifact a type may read."""
    root = tmp_path / "проект с пробелами" / "repo"
    agentic = root / ".agentic"
    for name in ("inbox", "outbox", "logs", "handoff", "roles", "phases", "doctrine", "pipelines"):
        (agentic / name).mkdir(parents=True)
    if context:
        (agentic / "context").mkdir()
    _write(agentic / "config.yaml", "project:\n  name: T\n" + STAGE_TYPES)
    _write(agentic / "inbox" / f"{TODO_ID}.md", TODO_TEXT)
    _write(
        agentic / "context" / f"PIPELINE-{TODO_ID}.yaml",
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n  description: plan the unit\n"
        "- name: agent-implementer\n  role: agent-implementer\n  task: 'Implement the unit.'\n"
        "- name: verify\n  role: supervisor\n",
    )
    _write(agentic / "context" / f"BASELINE-{TODO_ID}.sha", "deadbeef\n")
    _write(agentic / "outbox" / f"DONE-{TODO_ID}.md", "# DONE — sample\n")
    _write(agentic / "outbox" / f"DONE-{TODO_ID}.json", '{"files_changed": ["awf/a.py"], "status": "done"}')
    _write(
        agentic / "outbox" / f"REVIEW-REJECTED-{TODO_ID}.md",
        "# REVIEW\n\n## REVIEW-фикс\n\nR-2: MAJOR — поправить границу.\n",
    )
    _write(agentic / "handoff" / f"spec-context-{TODO_ID}.md", "# spec-context handoff\n")
    _write(agentic / "handoff" / f"agent-implementer-{TODO_ID}.md", "# implementer handoff\n")
    _write(agentic / "roles" / "agent-implementer.md", "# Роль\n\n## Instructions\nДелай работу.\n")
    _write(agentic / "roles" / "agent-qa-review.md", "# Роль\n\n## Инструкции\nИщи баги.\n")
    _write(agentic / "roles" / "supervisor.md", "# Роль\n\n## Роль\nВеди пайплайн.\n")
    _write(agentic / "roles" / "agent-system-analyst.md", "# Роль\n\n## Инструкции\nПиши требования.\n")
    _write(agentic / "phases" / "plan.md", "# Фазы\n\n1. Одна фаза.\n")
    _write(agentic / "doctrine" / "01-x.md", "# Доктрина\n\nТекст доктрины.\n")
    _write(root / "VISION.md", "# Видение\n\nПродукт.\n")
    return root


def _build(root: Path, stage: str, attempt: int = 1):
    family, role = TYPES[stage]
    return build_stage_context(root, TODO_ID, stage, role, attempt=attempt)


def test_T1_block_set_and_order_per_type(tmp_path: Path) -> None:
    root = project(tmp_path)
    for stage, (family, _role) in TYPES.items():
        result = _build(root, stage)
        ids = [block.id for block in result.blocks if block.full()]
        expected = [bid for bid in FAMILIES[family] if bid in ids]
        assert ids == expected, stage
        assert {"AUDIT", "METRICS", "MISSING"}.isdisjoint(ids)
    checklist = _build(root, "checklist-close")
    assert {"ROLE", "DOCTRINE", "REVIEW", "PREV", "PREVLINK"}.isdisjoint(
        {block.id for block in checklist.blocks}
    )


def test_T14_limit_sums_are_under_the_cap() -> None:
    expected = {
        "plan": 5600,
        "execute": 9000,
        "reviewer": 7900,
        "reviewer/checklist": 5150,
        "supervisor": 9000,
        "spec": 9000,
    }
    for family, total in expected.items():
        limits = LIMITS[family]
        assert sum(limits.values()) == total, family
        assert sum(limits[block] for block in FAMILIES[family]) <= CAP
        assert set(FAMILIES[family]) == set(limits)


def test_T12_absolute_project_path_in_every_type(tmp_path: Path) -> None:
    root = project(tmp_path)
    for stage in TYPES:
        text = _build(root, stage).text
        assert str(root.resolve()) in text


def test_T23_missing_artifact_is_a_line_not_a_failure(tmp_path: Path) -> None:
    root = project(tmp_path)
    (root / ".agentic" / "outbox" / f"DONE-{TODO_ID}.md").unlink()
    (root / ".agentic" / "handoff" / f"agent-implementer-{TODO_ID}.md").unlink()
    result = _build(root, "agent-implementer")
    assert result.missing["DONE"] == "DONE-отчёта нет"
    assert "нет данных: DONE-отчёта нет — " in result.text
    assert result.missing["PREVLINK"] == "других handoff-ов нет"
    assert "CTX-FAIL" not in result.text


def test_T24_empty_and_absent_blocks_have_different_reasons(tmp_path: Path) -> None:
    root = project(tmp_path)
    _write(root / ".agentic" / "outbox" / f"DONE-{TODO_ID}.md", "")
    result = _build(root, "agent-implementer")
    assert result.missing["DONE"] == "блок пуст"
    (root / ".agentic" / "outbox" / f"DONE-{TODO_ID}.md").unlink()
    again = _build(root, "agent-implementer")
    assert again.missing["DONE"] == "DONE-отчёта нет"


def test_T25_no_context_dir_is_ctx_fail_and_creates_nothing(tmp_path: Path) -> None:
    root = project(tmp_path)
    shutil.rmtree(root / ".agentic" / "context")
    with pytest.raises(StageContextError) as exc:
        _build(root, "agent-implementer")
    assert "CTX-FAIL" in str(exc.value)
    assert not (root / ".agentic" / "context").exists()


def test_T17_p0_overflow_is_ctx_fail_and_writes_no_file(tmp_path: Path) -> None:
    root = project(tmp_path)
    todo = root / ".agentic" / "inbox" / f"{TODO_ID}.md"
    todo.write_text(TODO_TEXT.replace("## Границы", "## Границы\n\n" + "x" * 12_000), encoding="utf-8")
    with pytest.raises(StageContextError) as exc:
        _build(root, "agent-implementer")
    assert "CTX-FAIL" in str(exc.value)
    assert not list((root / ".agentic" / "context").glob("STAGE-*.md"))


def test_fit_blocks_never_cuts_p0_and_reports_failure() -> None:
    blocks = [Block(id="CRIT", text="x" * 10_000, limit=2500)]
    text, cut, failed = fit_blocks(blocks, CAP)
    assert text == ""
    assert failed == ["P0"]
    assert cut == []

    fits = [Block(id="CRIT", text="x" * 3000, limit=2500)]
    text, _cut, failed = fit_blocks(fits, CAP)
    assert failed == []
    assert text == "x" * 3000


def test_T36_file_equals_text_and_ctx_names_it(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    assert result.file.is_file()
    assert result.file.read_text(encoding="utf-8") == result.text
    assert result.file.name == f"STAGE-{TODO_ID}-agent-implementer.md"
    log = (root / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")
    line = next(line for line in log.splitlines() if "CTX " in line)
    assert f"file={result.file}" in line
    assert f"total={len(result.text)}" in line
    assert "cap=9000" in line
    assert "type=execute" in line
    assert "role=" in line


def test_T42_ctx_line_for_every_type(tmp_path: Path) -> None:
    root = project(tmp_path)
    for stage, (family, _role) in TYPES.items():
        result = _build(root, stage)
        line = result.ctx_line(TODO_ID, stage, 1)
        assert f"type={family}" in line
        assert "cap=9000" in line
        assert f"total={len(result.text)}" in line
        assert "attempt=1" in line
        assert "missing=" in line


def test_T31_build_uses_the_role_file(tmp_path: Path) -> None:
    root = project(tmp_path)
    role = root / ".agentic" / "roles" / "agent-implementer.md"
    role.write_text("# Роль\n\n## Instructions\nСПЕЦ-РЕЖИМ-МАРКЕР\n", encoding="utf-8")
    assert "СПЕЦ-РЕЖИМ-МАРКЕР" in _build(root, "agent-implementer").text


def test_config_is_read_from_the_project(tmp_path: Path) -> None:
    root = project(tmp_path)
    assert load_config(root)["stage_types"]["agent-implementer"] == "execute"


def test_contract_block_below_a_comment_is_still_read(tmp_path: Path) -> None:
    """A TODO usually opens with a comment above ``---`` (all live ones do).

    The dispatcher reads such a unit as contracted; the stage must get the
    same answer, or ``CRIT`` loses ``verify:`` and ``STAGE`` loses ``files:``.
    """
    root = project(tmp_path)
    todo = root / ".agentic" / "inbox" / f"{TODO_ID}.md"
    todo.write_text("<!-- role_hint: agent-implementer -->\n" + TODO_TEXT, encoding="utf-8")
    text = _build(root, "agent-implementer").text
    assert "pytest -q tests/unit/test_a.py" in text
    assert "awf/a.py" in text


def test_fit_cuts_the_role_class_and_measures_separators() -> None:
    """R5: P2 ``ROLE`` is cut after P3 and before P1; ``total`` counts joints.

    Without ``ROLE`` in ``CUT_ORDER`` an overflow past P3 ended as ``cap`` —
    a ``CTX-FAIL`` although a cuttable block was still there. And the cap
    must be checked on the JOINED text: two characters per joint otherwise
    let ``total`` pass 9 000.
    """
    blocks = [
        Block(id="CRIT", text="c" * 3000, limit=2500),
        Block(id="ROLE", text="r" * 3000, limit=3000),
        Block(id="OUTPUT", text="o" * 800, limit=800),
    ]
    text, cut, failed = fit_blocks(blocks, 4000)
    assert failed == []
    assert cut == ["ROLE"]
    assert len(text) <= 4000


def test_role_coverage_comes_from_fields(tmp_path: Path) -> None:
    root = project(tmp_path)
    result = _build(root, "agent-implementer")
    role = next(block for block in result.blocks if block.id == "ROLE")
    assert role.total > 0
    assert (result.role_taken, result.role_total) == (role.taken, role.total)
    assert result.role_pct == (role.taken * 100) // role.total


def test_block_never_outgrows_its_limit_even_with_a_long_path() -> None:
    """The marker carries the artifact PATH, not a fixed 80 chars.

    With a constant reserve a block cut to 350 chars ended at 389 when its
    source path was long — the share the cap is summed from was overrun.
    """
    from awf.bounded_prompt import fit_to_limit

    source = "/tmp/opencode/" + "дерево-проекта/" * 8 + "REVIEW-TODO-0400.md"
    block = Block(
        id="REVIEW",
        text="\n\n".join(f"R-{index}: MINOR — " + "x" * 120 for index in range(20)),
        limit=350,
        source=source,
    )
    fit_to_limit(block)
    assert len(block.full()) <= 350
    assert "ещё " in block.text
    assert source in block.text


def test_missing_artifact_line_is_never_cut() -> None:
    """AC9: «нет данных: причина — путь» is the record that an input was absent.

    Cutting it left a bare marker ("… ещё 1: см. …") and the stage no longer
    knew which artifact was missing — exactly what AC9 forbids.
    """
    absent = Block(
        id="BLOCKED",
        text="нет данных: BLOCKED-отчёта нет — /p/.agentic/outbox/BLOCKED-TODO-0400.md",
        limit=100,
        source="/p/.agentic/outbox/BLOCKED-TODO-0400.md",
        missing="BLOCKED-отчёта нет",
    )
    blocks = [
        Block(id="CRIT", text="c" * 2000, limit=2500),
        Block(id="ROLE", text="r" * 2000, limit=2000),
        absent,
    ]
    text, cut, failed = fit_blocks(blocks, 3900)
    assert failed == []
    assert "BLOCKED" not in cut
    assert absent.full() in text
    assert cut == ["ROLE"]


def test_unit_is_the_contract_not_the_growing_todo(tmp_path: Path) -> None:
    """AC3/AC36: the TODO grows with fix rounds, the unit block may not.

    ``hygiene.review_append`` adds a ``## REVIEW-фикс`` round to the TODO;
    a whole-file P0 block then took the cap on its own (TODO-0357 = 16 369
    chars) and returned ``CTX-FAIL`` on the units AC3 names. The contract
    block keeps UNIT bounded and the round belongs to REVIEW.
    """
    root = project(tmp_path)
    todo = root / ".agentic" / "inbox" / f"{TODO_ID}.md"
    round_text = "R-1: MAJOR — подробное замечание.\n" + "z" * 15_000
    todo.write_text(
        TODO_TEXT + f"\n\n## REVIEW-фикс (супервизор, виток 1)\n\n{round_text}",
        encoding="utf-8",
    )
    result = _build(root, "agent-implementer")
    assert len(result.text) <= CAP
    unit = next(block for block in result.blocks if block.id == "UNIT")
    review = next(block for block in result.blocks if block.id == "REVIEW")
    assert "Добавить функцию" in unit.text
    assert "z" * 100 not in unit.text
    assert "R-1" in review.text or "R-1" in review.tail
