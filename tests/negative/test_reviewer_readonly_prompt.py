"""A83: «ревью не правит код» reaches the stage prompt, not only the role.

The role file is cut by ``ROLE``'s own budget, so a rule that lives only
there can be lost. ``RULES`` is P0 and always arrives: for the ``reviewer``
and ``supervisor`` families it carries the read-only line instead of the
"edits only inside the unit" one, so the 350-character budget does not grow.

Owns the unit's acceptance facts:

- ``reviewer`` text has ``ревью не правит код`` and ``findings``;
- ``supervisor`` text has them too;
- ``execute`` text does not — it authors code;
- the ``RULES`` block of a review family stays within 350 characters;
- the prompt on the real-unit fixture stays within ``CAP``;
- all four review role templates carry the same line.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf.stage_context import CAP, build_stage_context

TODO_ID = "TODO-0400"
REPO = Path(__file__).resolve().parents[2]
PHRASE = "ревью не правит код"

STAGE_TYPES = """\
stage_types:
  plan: plan
  agent-implementer: execute
  agent-qa-review: reviewer
  verify: supervisor
  spec-design: spec
"""

TODO_TEXT = """\
---
files: ["awf/a.py"]
verify: ["pytest -q tests/unit/test_a.py"]
gates: ["tests"]
---
# TODO — sample

## Что сделать

1. Добавить функцию `f`.

## Приёмка (факты)

- AC1: `f()` возвращает 1.

## Границы

- `awf/other.py` не трогать.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path, *, todo: str = TODO_TEXT) -> Path:
    root = tmp_path / "repo"
    agentic = root / ".agentic"
    for name in ("inbox", "outbox", "logs", "context", "handoff", "roles", "doctrine"):
        (agentic / name).mkdir(parents=True)
    _write(agentic / "config.yaml", f"project:\n  name: T\n{STAGE_TYPES}")
    _write(agentic / "inbox" / f"{TODO_ID}.md", todo)
    _write(
        agentic / "context" / f"PIPELINE-{TODO_ID}.yaml",
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n  description: plan\n"
        "- name: agent-implementer\n  role: agent-implementer\n  task: 'Do it.'\n"
        "- name: agent-qa-review\n  role: agent-qa-review\n  task: 'Review.'\n"
        "- name: verify\n  role: supervisor\n",
    )
    _write(agentic / "context" / f"BASELINE-{TODO_ID}.sha", "deadbeef\n")
    return root


def _rules(result) -> str:
    block = next(b for b in result.blocks if b.id == "RULES")
    return block.full()


@pytest.mark.parametrize(
    ("stage_name", "role"),
    (("agent-qa-review", "agent-qa-review"), ("verify", "supervisor")),
)
def test_review_families_carry_the_readonly_line(tmp_path, stage_name, role):
    root = _project(tmp_path)
    result = build_stage_context(root, TODO_ID, stage_name, role)

    assert PHRASE in result.text, result.text
    assert "findings" in result.text
    rules = _rules(result)
    assert PHRASE in rules, rules
    assert "findings" in rules, rules


def test_execute_family_does_not_get_the_readonly_line(tmp_path):
    root = _project(tmp_path)
    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    )

    assert PHRASE not in result.text
    assert PHRASE not in _rules(result)


def test_review_rules_keep_the_350_budget(tmp_path):
    root = _project(tmp_path)
    for stage_name, role in (
        ("agent-qa-review", "agent-qa-review"),
        ("verify", "supervisor"),
    ):
        result = build_stage_context(root, TODO_ID, stage_name, role)
        assert len(_rules(result)) <= 350, stage_name
        # The line that must survive the swap.
        assert "прошлая сессия не источник фактов" in _rules(result)


def test_real_unit_prompt_fits_the_cap(tmp_path):
    todo = REPO / "tests" / "fixtures" / "todo-real-execute.md"
    assert todo.is_file(), f"fixture missing: {todo}"
    root = _project(tmp_path, todo=todo.read_text(encoding="utf-8"))

    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    )
    assert len(result.text) <= CAP
    assert "CTX-FAIL" not in result.text


def test_all_review_templates_name_the_readonly_rule():
    root = REPO / "awf" / "templates" / "roles"
    for name in (
        "agent-architect-review.md",
        "agent-qa-review.md",
        "agent-project-auditor.md",
        "agent-requirements-reviewer.md",
    ):
        text = (root / name).read_text(encoding="utf-8")
        assert PHRASE in text, name
        assert "findings" in text, name
        # The line sits at the top, before the first section of the role.
        assert text.index(PHRASE) < text.index("\n## "), name
