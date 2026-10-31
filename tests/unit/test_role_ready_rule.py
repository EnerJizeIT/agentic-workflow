"""A12: every DONE-writing role template states the `.ready` signal rule.

The rule is one line, word for word, in the template and in its
``.agentic/roles`` copy. ``agent-qa-review`` is the single exception: its
verdict can be BLOCKED, so the rule carries a condition (CTX1) — an
unconditional "write DONE-<todo>.ready" would let a blocked QA signal
success.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SIGNAL_RULE = (
    "Сигнал стадии — пустой `DONE-{todo}.ready`: создай его ПОСЛЕ "
    "`DONE-{todo}.md` и `DONE-{todo}.json`; без него движок ждёт 30 с и "
    "запускает лишний виток."
)
# CTX1: same rule, but only for a non-BLOCKED verdict.
QA_SIGNAL_RULE = (
    "Сигнал стадии — пустой `DONE-{todo}.ready`, если вердикт не BLOCKED: "
    "создай его ПОСЛЕ `DONE-{todo}.md` и `DONE-{todo}.json`; без него движок "
    "ждёт 30 с и запускает лишний виток."
)
# CTX1 витка 3: у роли-ревьюера требований стадия уходит в откат по
# REVIEW-REJECTED, поэтому её правило тоже условное — но по своей развилке.
REQ_SIGNAL_RULE = (
    "Сигнал стадии зависит от вердикта: `PASS` и `READY` — пустой "
    "`DONE-{todo}.ready` (создай его ПОСЛЕ `DONE-{todo}.md` и "
    "`DONE-{todo}.json`); `FAIL` и `BLOCKED` готовности — "
    "`REVIEW-REJECTED-{todo}.ready`, пакет уходит автору, `DONE` не пишется; "
    "сомнение в контракте — `BLOCKED-{todo}.ready`, эскалация супервизору; "
    "без сигнала движок ждёт 30 с и запускает лишний виток."
)
QA_ROLE = "agent-qa-review.md"
REQ_ROLE = "agent-requirements-reviewer.md"
# Роли, чья стадия имеет альтернативный исход: правило условное.
CONDITIONAL = {QA_ROLE: QA_SIGNAL_RULE, REQ_ROLE: REQ_SIGNAL_RULE}
ROOT = Path(__file__).resolve().parents[2]


def _done_writing_roles() -> list[str]:
    roles = []
    for path in sorted((ROOT / "awf" / "templates" / "roles").glob("agent-*.md")):
        if "DONE-" in path.read_text(encoding="utf-8"):
            roles.append(path.name)
    return roles


def test_every_done_writing_role_template_states_the_ready_rule():
    roles = _done_writing_roles()
    assert roles, "no agent role templates found"
    assert QA_ROLE in roles
    for name in roles:
        template = ROOT / "awf" / "templates" / "roles" / name
        text = template.read_text(encoding="utf-8")
        expected = CONDITIONAL.get(name, SIGNAL_RULE)
        assert expected in text, name


def test_qa_role_rule_is_conditional_on_the_verdict():
    """CTX1: безусловная строка в QA-роли разрешила бы DONE-сигнал при BLOCKED."""
    text = (ROOT / "awf" / "templates" / "roles" / QA_ROLE).read_text(
        encoding="utf-8"
    )
    assert SIGNAL_RULE not in text, "the QA rule must carry the verdict condition"
    assert QA_SIGNAL_RULE in text
    assert "если вердикт не BLOCKED" in text
    assert "ТОЛЬКО `BLOCKED-{todo}.ready`" in text


def test_signal_rule_does_not_drift_between_template_and_copy():
    copies = ROOT / ".agentic" / "roles"
    if not copies.is_dir():
        pytest.skip("no project role copies here (.agentic/ is runtime, not tracked)")
    for name in _done_writing_roles():
        expected = CONDITIONAL.get(name, SIGNAL_RULE)
        template = (ROOT / "awf" / "templates" / "roles" / name).read_text(
            encoding="utf-8"
        )
        copy = copies / name
        assert copy.is_file(), f"missing project copy: {copy}"
        assert expected in copy.read_text(encoding="utf-8"), name
        assert expected in template, name
        if name in CONDITIONAL:
            assert SIGNAL_RULE not in copy.read_text(encoding="utf-8"), name


def test_requirements_reviewer_rule_names_the_rollback_signal():
    """CTX1 витка 3: FAIL идёт в откат, а не в DONE — иначе откат не сработает."""
    for base in (ROOT / "awf" / "templates" / "roles", ROOT / ".agentic" / "roles"):
        path = base / REQ_ROLE
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert REQ_SIGNAL_RULE in text, path
        assert "REVIEW-REJECTED-{todo}.ready" in text, path
        assert SIGNAL_RULE not in text, path


def test_requirements_reviewer_rule_covers_the_readiness_verdict():
    """QA-0312-01: у роли ДВА набора вердиктов, и второй тоже двигает стадию.

    Строка правила, знающая только `PASS`/`FAIL`, оставляла `BLOCKED`
    готовности без сигнала. Воркер писал бы `DONE-{todo}.ready` (пакет с
    блокером уходил дальше как готовый) или `BLOCKED-{todo}.ready` (эскалация
    супервизору), тогда как пайплайн ждёт `REVIEW-REJECTED`, чтобы вернуть
    пакет автору. Тест ловит ровно эту дыру: в правиле есть `READY` и есть
    связка «`BLOCKED` готовности → `REVIEW-REJECTED`».
    """
    for base in (ROOT / "awf" / "templates" / "roles", ROOT / ".agentic" / "roles"):
        path = base / REQ_ROLE
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "`READY`" in text, path
        assert "`BLOCKED` готовности — `REVIEW-REJECTED-{todo}.ready`" in text, path


def test_shipped_pipelines_roll_back_a_rejected_requirements_review():
    """Правило роли «BLOCKED готовности → REVIEW-REJECTED» верно только пока
    пайплайн объявляет `on_rejected: rollback_to:...` для этой роли.

    Сверка стороны роли со стороной пайплайна: если шаблон станет
    `escalate`, текст роли начнёт врать, и тест это назовёт.
    """
    from awf.pipeline import load_stages

    checked = 0
    for path in sorted((ROOT / "awf" / "templates" / "pipelines").glob("*.yaml")):
        for stage in load_stages(path):
            if stage.role != "agent-requirements-reviewer":
                continue
            checked += 1
            assert stage.on_rejected.startswith("rollback_to:"), (
                f"{path.name}:{stage.name} on_rejected={stage.on_rejected!r} — "
                "the role's REVIEW-REJECTED rule points at a rollback"
            )
    assert checked, "no packaged pipeline uses agent-requirements-reviewer"
