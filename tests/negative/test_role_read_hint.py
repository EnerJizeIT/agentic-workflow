"""A72: the read hint reaches the stage through the role templates.

Split out of ``test_stage_context_pack.py`` (ratchet 400): the pack owns the
builder's contract, this file owns the one policy line the unit added.

- the ``**Чтение:**`` hint sits in the header of the four shipped role
  templates and survives the ``ROLE`` cut of the ``execute`` and ``reviewer``
  families — checked through the real builder, not through the template file;
- the real-unit execute prompt stays under 9 000 with the stage tail in.

``RULES`` is deliberately out of scope here: the block keeps its original
text byte for byte (``прошлая сессия не источник фактов`` is documented and
asserted by ``test_orchestrator``), so the hint is a role-template rule only.

The project fixture and its helper come from ``test_stage_context_pack`` —
one copy, so a family added to the pack's config cannot miss this file.
"""
from __future__ import annotations

from awf import agent_stage
from awf.pipeline import Stage
from awf.stage_context import CAP, build_stage_context
from awf.stage_context_roles import HEADER_LIMIT
from tests.negative.test_stage_context_pack import (
    REPO,
    TODO_ID,
    _project,
    _write,
)

#: The four roles the unit ships the hint in (TODO-0397, item 2).
TEMPLATES = (
    "agent-implementer",
    "agent-qa-review",
    "agent-architect-review",
    "agent-project-auditor",
)

#: The two families those roles run in; ``supervisor`` is not one of them.
FAMILIES = (
    ("agent-implementer", "execute"),
    ("agent-qa-review", "reviewer"),
)


def test_read_hint_reaches_execute_and_reviewer_roles(tmp_path):
    """The hint must survive the ROLE cut, not merely exist in the file."""
    root = _project(tmp_path)
    for name in TEMPLATES:
        template = REPO / "awf" / "templates" / "roles" / f"{name}.md"
        text = template.read_text(encoding="utf-8")
        # ``select_role_core`` clips the header at its limit with no marker: a
        # header over it loses its tail silently, so the hint would stay while
        # the description behind it is cut mid-word.
        assert len(text[: text.index("## ")]) <= HEADER_LIMIT, name
        _write(root / ".agentic" / "roles" / f"{name}.md", text)

        for stage_name, family in FAMILIES:
            result = build_stage_context(root, TODO_ID, stage_name, name)
            # The table above names the family; without this line the test
            # passes on any fallback family the stage happens to resolve to.
            assert result.family == family, (name, stage_name, result.family)
            role = next(block for block in result.blocks if block.id == "ROLE")
            assert "инструментом read" in role.full(), (name, family, role.taken)
            assert "не перечитывай" in role.full(), (name, family, role.taken)


def test_read_hint_survives_the_supervisor_family(tmp_path):
    """The supervisor family does not cut the hint out of a role that has it.

    The production supervisor role is ``awf/templates/roles/supervisor.md``,
    which is not one of the four roles this unit ships the hint in; this test
    proves the ``supervisor`` family itself (ROLE share 1 000) delivers the
    line when the role carries it. The production-role gap is reported in
    DONE with numbers — no skip, because the templates below are mandatory.
    """
    root = _project(tmp_path)
    for name in TEMPLATES:
        template = REPO / "awf" / "templates" / "roles" / f"{name}.md"
        assert template.is_file(), f"role template missing: {template}"
        _write(root / ".agentic" / "roles" / f"{name}.md",
               template.read_text(encoding="utf-8"))
        result = build_stage_context(root, TODO_ID, "verify", name)
        assert result.family == "supervisor", (name, result.family)
        role = next(block for block in result.blocks if block.id == "ROLE")
        assert "инструментом read" in role.full(), (name, role.taken)


def test_execute_fits_9000_on_the_real_todo(tmp_path):
    """The project's own unit is the fixture — TODO and role, tail included.

    Building without the execute suffix hid the blocker: on TODO-0383 the
    contract block alone rendered 5 288 characters against its 1 800 share,
    and the real stage refused with ``CTX-FAIL`` before the spawn. The
    suffix, the real unit and the real role file enter one measurement.

    The unit is a committed fixture, not ``.agentic/``: the inbox loses a
    closed unit and a live role is runtime state, so both used to make this
    test skip. A skip when ``skip_allow=0`` is a red gate, not a pass.
    """
    todo = REPO / "tests" / "fixtures" / "todo-real-execute.md"
    role = REPO / "awf" / "templates" / "roles" / "agent-implementer.md"
    assert todo.is_file(), f"fixture missing: {todo}"
    assert role.is_file(), f"role template missing: {role}"
    root = _project(tmp_path, todo=todo.read_text(encoding="utf-8"))
    _write(root / ".agentic" / "roles" / "agent-implementer.md",
           role.read_text(encoding="utf-8"))

    stage = Stage(name="agent-implementer", role="agent-implementer", kind="execute")
    suffix = "\n\n".join(
        part
        for part in (
            agent_stage._execute_contract(TODO_ID),
            agent_stage._stage_assignment_block(stage),
            agent_stage._build_pipeline_context(root, TODO_ID, None),
            '## Stage identity\nstage_id="agent-implementer", attempt=1',
        )
        if part
    )
    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer", suffix=suffix,
    )

    assert len(result.text) <= CAP
    assert "CTX-FAIL" not in result.text
    assert result.role_taken > 300
