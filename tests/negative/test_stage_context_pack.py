"""IMP2b: the stage is spawned from the builder — no ``--file``, one cap.

Owns the unit's acceptance facts:

- the argv carries no ``--file`` and the prompt is the written stage file;
- the ``CTX`` line names type, cap, file, role and coverage;
- ``execute`` fits 9 000 chars, ``verify``/``replan`` fit 6 000;
- the replan text carries the REVIEW findings and no handoff history;
- P0 that does not fit stops the stage before the subprocess;
- an old snapshot starts with ``CTX-TYPE-DEFAULT`` and is not rewritten;
- the prompt path never calls the old builders;
- every role keeps its phrases and a ``role_pct``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from awf import agent_stage
from awf.pipeline import Stage
from awf.stage_context import CAP, build_stage_context

TODO_ID = "TODO-0500"
REPO = Path(__file__).resolve().parents[2]

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

## REVIEW-фикс (супервизор, виток 1)

R-2: MAJOR — находка-маркер, поправить границу.
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _project(tmp_path: Path, *, todo: str = TODO_TEXT, stage_types: str = STAGE_TYPES) -> Path:
    root = tmp_path / "repo"
    agentic = root / ".agentic"
    for name in ("inbox", "outbox", "logs", "context", "handoff", "roles", "doctrine"):
        (agentic / name).mkdir(parents=True)
    _write(agentic / "config.yaml", f"project:\n  name: T\n{stage_types}")
    _write(agentic / "inbox" / f"{TODO_ID}.md", todo)
    _write(
        agentic / "context" / f"PIPELINE-{TODO_ID}.yaml",
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n  description: plan\n"
        "- name: agent-implementer\n  role: agent-implementer\n  task: 'Do it.'\n"
        "- name: verify\n  role: supervisor\n",
    )
    _write(agentic / "context" / f"BASELINE-{TODO_ID}.sha", "deadbeef\n")
    _write(agentic / "outbox" / f"DONE-{TODO_ID}.md", "# DONE\n\nmarker-done\n")
    _write(agentic / "roles" / "agent-implementer.md", "# Роль\n\n## Instructions\nРеализуй.\n")
    _write(agentic / "roles" / "supervisor.md", "# Роль\n\n## Роль\nВеди пайплайн.\n")
    _write(agentic / "handoff" / f"agent-qa-review-{TODO_ID}.md", "HANDOFF-BODY " * 200)
    return root


def test_argv_has_no_file_and_prompt_is_the_stage_file(tmp_path, monkeypatch):
    root = _project(tmp_path)
    captured: dict = {}

    def fake_run(cmd, cwd, **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)

    stage = Stage(name="agent-implementer", role="agent-implementer", kind="execute")
    agent_stage.run_agent_stage(
        stage, TODO_ID, root, {"models": {"agent-implementer": {"model": "p/test"}}},
        root / ".agentic" / "logs",
    )

    assert "--file" not in captured["cmd"]
    stage_file = root / ".agentic" / "context" / f"STAGE-{TODO_ID}-agent-implementer.md"
    assert stage_file.read_text(encoding="utf-8") == captured["cmd"][-1]


def test_ctx_line_has_type_cap_file_role(tmp_path):
    root = _project(tmp_path)
    build_stage_context(root, TODO_ID, "agent-implementer", "agent-implementer")

    log = (root / ".agentic" / "logs" / "orchestrator.log").read_text(encoding="utf-8")
    ctx = next(line for line in log.splitlines() if " CTX " in line)
    for field in ("type=execute", f"cap={CAP}", "total=", "file=", "role=", "role_pct=",
                  "cut=", "missing="):
        assert field in ctx, ctx


def test_execute_fits_9000_on_the_real_todo(tmp_path):
    """The project's own unit is the fixture — TODO and role, tail included.

    Building without the execute suffix hid the blocker: on TODO-0383 the
    contract block alone rendered 5 288 characters against its 1 800 share,
    and the real stage refused with ``CTX-FAIL`` before the spawn. The
    suffix, the real unit and the real role file enter one measurement.
    """
    todo = REPO / ".agentic" / "inbox" / "TODO-0383.md"
    if not todo.is_file():
        pytest.skip("real TODO-0383 is not in the inbox")
    root = _project(tmp_path, todo=todo.read_text(encoding="utf-8"))
    role = REPO / ".agentic" / "roles" / "agent-implementer.md"
    if role.is_file():
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
    assert result.role_taken > 300


def test_unit_and_crit_do_not_repeat_the_same_section(tmp_path):
    """One fact, one owner: ``UNIT`` keeps the task and borders, ``CRIT`` the
    acceptance criteria. Two copies cost 1 100 characters on TODO-0383."""
    root = _project(tmp_path)
    text = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    ).text

    assert text.count("## Приёмка") == 1
    assert text.count("## Границы") == 1


def test_replan_fits_6000_and_carries_findings(tmp_path):
    root = _project(tmp_path)
    result = build_stage_context(
        root, TODO_ID, "replan", "supervisor", cap=6000,
    )

    assert len(result.text) <= 6000
    assert result.cap == 6000
    assert "REVIEW" in result.text
    assert "находка-маркер" in result.text


def test_replan_has_no_handoff_history(tmp_path):
    root = _project(tmp_path)
    result = build_stage_context(root, TODO_ID, "replan", "supervisor", cap=6000)

    assert result.text.count("HANDOFF-BODY") <= 1


def test_replan_blocked_note_keeps_its_share(tmp_path):
    """§6 has ``BLOCKED`` = 100 in every row, the supervisor row included.

    The replan stage injects the note as an extra block; with no cell in
    ``LIMITS`` it rendered as an unbounded block (a missing cell means "no
    such block") and could eat the whole 6 000 cap before the findings.
    """
    root = _project(tmp_path)
    _write(
        root / ".agentic" / "outbox" / f"BLOCKED-{TODO_ID}.md",
        "BLOCKER-REASON " * 200,
    )

    result = build_stage_context(
        root, TODO_ID, "replan", "supervisor", cap=6000, extra=("BLOCKED",),
    )

    blocked = next(block for block in result.blocks if block.id == "BLOCKED")
    assert blocked.limit == 100
    # The share is a budget, not a hard clip: the cut marker carries the
    # artifact path and can be longer than 100 (the same rule the marker
    # already follows for REVIEW/PREVLINK). What must hold: the note never
    # enters whole, and the block names the file the stage can read.
    assert "BLOCKER-REASON " * 200 not in result.text
    assert str(root / ".agentic" / "outbox" / f"BLOCKED-{TODO_ID}.md") in blocked.full()


def test_p0_that_does_not_fit_never_spawns(tmp_path, monkeypatch):
    root = _project(tmp_path, todo="# TODO huge\n\n## Приёмка\n\n" + "x" * 10000 + "\n")
    spawned: list = []

    def fake_run(cmd, cwd, **kwargs):
        spawned.append(cmd)

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)

    stage = Stage(name="agent-implementer", role="agent-implementer", kind="execute")
    with pytest.raises(RuntimeError, match="CTX-FAIL"):
        agent_stage.run_agent_stage(
            stage, TODO_ID, root,
            {"models": {"agent-implementer": {"model": "p/test"}}},
            root / ".agentic" / "logs",
        )
    assert spawned == []


def test_old_snapshot_starts_with_type_default_and_is_not_rewritten(tmp_path, capsys):
    stage_types = "stage_types:\n  verify: supervisor\n"
    root = _project(tmp_path, stage_types=stage_types)
    snapshot = root / ".agentic" / "context" / f"PIPELINE-{TODO_ID}.yaml"
    before = snapshot.read_bytes()

    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    )

    assert result.family == "execute"
    assert "CTX-TYPE-DEFAULT" in capsys.readouterr().err
    assert snapshot.read_bytes() == before


def test_prompt_path_has_no_old_builder_calls():
    """Vitok 3: the old prompt homes are gone from the codebase."""
    forbidden = {"build_prompt", "resolve_prev_handoffs"}
    for path in (REPO / "awf").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, (path.name, name)
    for name in ("agent_stage.py", "supervisor.py"):
        tree = ast.parse((REPO / "awf" / name).read_text(encoding="utf-8"))
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert calls.isdisjoint(forbidden), (name, calls & forbidden)


def test_every_role_keeps_phrases_and_coverage(tmp_path):
    from awf.stage_context_roles import select_role_core
    from tests.unit.test_stage_context_roles import PHRASES

    roles = tmp_path / "roles"
    roles.mkdir()
    for name, phrases in PHRASES.items():
        _write(
            roles / name,
            f"# ROLE: {name}\n\n## Instructions\n{phrases[0]} — правило.\n\n"
            f"## Handoff contract\n{phrases[1]} — выход.\n\n"
            f"## Запреты\n{phrases[2]} — нельзя.\n",
        )

    for name, phrases in PHRASES.items():
        selection = select_role_core(
            (roles / name).read_text(encoding="utf-8"), "execute", 1200, source=str(roles / name)
        )
        lowered = selection.text.lower()
        assert sum(1 for phrase in phrases if phrase.lower() in lowered) >= 3, name
        assert selection.total > 0

    root = _project(tmp_path)
    for role_name in PHRASES:
        _write(root / ".agentic" / "roles" / role_name, (roles / role_name).read_text(encoding="utf-8"))
    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    )
    assert result.role_total > 0
    assert 0 <= result.role_pct <= 100


def test_role_block_skips_the_yaml_front_matter(tmp_path):
    """The role's ``description:`` is metadata, never the role core.

    Real role files open with YAML whose description may contain ``##``
    text; before the strip the core's "header" was the first 500 characters
    of YAML, so a cut ``ROLE`` block delivered a fragment of the front
    matter instead of the instructions.
    """
    root = _project(tmp_path)
    _write(
        root / ".agentic" / "roles" / "agent-implementer.md",
        '---\nname: agent-implementer\ndescription: "… НЕ для ## Instructions"\n---\n'
        "# Роль\n\n## Instructions\nРеализуй задачу строго по границам.\n",
    )

    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
    )
    role = next(block for block in result.blocks if block.id == "ROLE")

    assert role.full().startswith("# Роль")
    assert "Реализуй задачу строго по границам." in role.full()
    assert "name: agent-implementer" not in role.full()


def test_suffix_is_capped_at_1500(tmp_path):
    """Vitok 5: static guidance (tail) never eats the unit's own budget."""
    from awf.bounded_prompt import SUFFIX_MAX

    root = _project(tmp_path)
    result = build_stage_context(
        root, TODO_ID, "agent-implementer", "agent-implementer",
        suffix="tail-marker " * 400,
    )
    assert SUFFIX_MAX == 1500
    assert len(result.text) <= CAP
    assert "tail-marker" in result.text
    assert result.text.count("tail-marker") <= 125  # 12 chars × 125 = 1500
