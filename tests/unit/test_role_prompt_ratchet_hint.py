"""TODO-0356: the implementer stage is told the 400-line file budget.

The ratchet counter ``files_over_400`` (baseline 136) fails a unit that
adds a file longer than 400 lines. Four units overnight (0348, 0349, 0351,
0354) did the work right and then spent an extra round splitting the file —
the rule was nowhere in the stage prompt. The rule lives in the role
template, which the engine attaches to the worker with ``--file``; these
tests read the ASSEMBLED stage text (prompt + attachments), not the role
file in isolation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from awf import agent_stage
from awf.api import roles
from awf.pipeline import Stage

ROOT = Path(__file__).resolve().parents[2]
RULE = (
    "Новый модуль и новый тест-файл — **≤ 400 строк**; если не влезает, "
    "дели по смыслу сразу, а не после отказа гейта; `.ratchet-baseline` "
    "не трогать."
)


def _flat(text: str) -> str:
    """Whitespace-collapsed text: the rule is line-wrapped in the file."""
    return re.sub(r"\s+", " ", text)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "stage_types:\n  qa: execute\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO — the unit\n\nGoal: keep the file small.\n", encoding="utf-8"
    )
    template = roles._builtin_role_template("agent-implementer")
    assert template is not None, "shipped implementer template is missing"
    role_file = project / ".agentic" / "roles" / "agent-implementer.md"
    role_file.parent.mkdir(parents=True)
    role_file.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return project


def _capture_run(monkeypatch) -> dict:
    captured: dict = {}

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None,
                 logs_dir=None, hard_timeout=None, env=None, signal_holder=None,
                 **kwargs):
        captured["cmd"] = cmd

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True,
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return captured


def _assembled_stage_text(cmd: list[str]) -> str:
    """What the worker reads: the prompt plus every ``--file`` attachment."""
    parts = [cmd[-1]]
    for index, arg in enumerate(cmd[:-1]):
        if arg == "--file":
            parts.append(Path(cmd[index + 1]).read_text(encoding="utf-8"))
    return _flat("\n".join(parts))


def test_assembled_implementer_prompt_states_the_400_line_budget(tmp_path, monkeypatch):
    captured = _capture_run(monkeypatch)
    project = _project(tmp_path)
    stage = Stage(name="agent-implementer", role="agent-implementer", kind="execute")

    agent_stage.run_agent_stage(
        stage, "TODO-0001", project,
        {"models": {"worker": {"model": "p/test"}}},
        project / ".agentic" / "logs",
    )

    assert _flat(RULE) in _assembled_stage_text(captured["cmd"])


def test_project_role_copy_carries_the_same_rule():
    """The live copy wins over the template — it must not drop the rule."""
    copy = ROOT / ".agentic" / "roles" / "agent-implementer.md"
    if not copy.is_file():
        pytest.skip("no project role copy here (.agentic/ is runtime, not tracked)")
    assert _flat(RULE) in _flat(copy.read_text(encoding="utf-8"))


def test_add_role_materializes_the_rule_into_the_project_copy(tmp_path):
    """The delivery path: ``add_role`` writes the project copy from the template.

    The first test hand-writes the copy, so it would stay green even if
    ``add_role`` stopped shipping the rule with the built-in template.
    """
    from awf.supervisor import resolve_role_file

    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True)
    roles.add_role(project, "agent-implementer")

    role_file = resolve_role_file("agent-implementer", project)
    assert role_file == project / ".agentic" / "roles" / "agent-implementer.md"
    assert _flat(RULE) in _flat(role_file.read_text(encoding="utf-8"))


def test_live_role_copy_does_not_order_the_opposite():
    """Every ``.ratchet-baseline`` mention must be a negation.

    A positive order to raise the baseline (the earlier wording of step 5
    in this repo's copy) next to "do not touch it" gave the worker two
    opposite instructions — the exact situation the unit exists to remove.
    """
    copy = ROOT / ".agentic" / "roles" / "agent-implementer.md"
    if not copy.is_file():
        pytest.skip("no project role copy here (.agentic/ is runtime, not tracked)")
    flat = _flat(copy.read_text(encoding="utf-8"))
    mentions = [part for part in re.split(r"[.;]", flat) if "ratchet-baseline" in part]
    assert mentions, "the live copy lost the rule's baseline mention"
    for part in mentions:
        assert "не " in part, f"positive order to raise the baseline: {part!r}"
