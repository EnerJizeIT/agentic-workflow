"""IMP6 (TODO-0344): the wait-mode fact and the one waiting phrase.

R6 / AC22–AC25: ``awf_wait_for_event`` answers ``run_active`` (забег vs
reactive), its docstring holds ONE rule, and ``awf_status``/``awf_brief``
name the same waiting phrase for the same state.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from awf import api, run_state
from awf.pipeline_state import write_state


@pytest.fixture
def awf_project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    return tmp_git_repo


def _live_single_start(project, monkeypatch):
    """A pipeline is running WITHOUT a run record (single ``awf_start``)."""
    write_state(
        project,
        stage_name="agent-implementer",
        stage_kind="execute",
        stage_idx=2,
        pipeline_pid=os.getpid(),
    )
    monkeypatch.setattr(
        "awf.api._liveness.read_cmdline",
        lambda pid: f"python -m awf start --project-dir {project}",
    )


def _running_project(project, monkeypatch):
    """Work history + a live pipeline on the execute stage.

    Without history the card falls back to the setup chain and never
    reaches the waiting phrase.
    """
    done = project / ".agentic" / "done" / "TODO-0001"
    done.mkdir(parents=True)
    pipelines = project / ".agentic" / "pipelines"
    pipelines.mkdir(parents=True, exist_ok=True)
    (pipelines / "default.yaml").write_text(
        "name: default\n"
        "stages:\n"
        "- {name: plan, role: supervisor}\n"
        "- {name: agent-implementer, role: agent-implementer}\n"
        "- {name: verify, role: supervisor}\n",
        encoding="utf-8",
    )
    _live_single_start(project, monkeypatch)


class TestRunActiveField:
    """AC23: the answer names the mode; it is not ``pipeline_running``."""

    def test_single_start_is_reactive(self, awf_project, monkeypatch):
        _live_single_start(awf_project, monkeypatch)
        assert api.get_status(awf_project).pipeline_running is True

        result = api.wait_for_event(awf_project, timeout=0)

        assert result.run_active is False
        assert result.as_dict()["run_active"] is False

    def test_active_run_is_a_run(self, awf_project, monkeypatch):
        _live_single_start(awf_project, monkeypatch)
        run_state.write_run(
            awf_project,
            active=True,
            queue=["TODO-0001"],
            current="TODO-0001",
        )

        result = api.wait_for_event(awf_project, timeout=0)

        assert result.run_active is True
        assert result.as_dict()["run_active"] is True


class TestWaitRuleInDocstring:
    """AC22/AC25: one rule, not two contradicting ones."""

    def test_no_contradicting_pair(self):
        from agent_workflow_ui.tools import awf as tools

        doc = " ".join((tools.awf_wait_for_event.__doc__ or "").split())

        assert not (
            "Do NOT call this in a loop" in doc
            and "DOES wait in a loop" in doc
        )

    def test_rule_phrases_are_named(self):
        from agent_workflow_ui.tools import awf as tools

        doc = tools.awf_wait_for_event.__doc__ or ""

        assert "wait only inside an active run" in doc
        assert "react to the user, no loop" in doc


class TestOneWaitingPhrase:
    """AC22: status and brief name the same phrase for the same state."""

    def _running_project(self, awf_project, monkeypatch):
        _running_project(awf_project, monkeypatch)

    def test_status_and_brief_agree(self, awf_project, monkeypatch):
        from agent_workflow_ui.tools import awf as tools

        self._running_project(awf_project, monkeypatch)

        status = asyncio.run(tools.awf_status(str(awf_project)))
        brief = asyncio.run(tools.awf_brief(str(awf_project)))

        assert status["status"] == "ok"
        assert brief["status"] == "ok"
        assert status["next_action"] == brief["next_action"]
        assert "wait only inside an active run" in status["next_action"]


class TestClosedRunField:
    """AC23/INV5: a closed run record is not an active run."""

    def test_closed_run_is_not_a_run(self, awf_project, monkeypatch):
        _live_single_start(awf_project, monkeypatch)
        run_state.write_run(awf_project, active=False)

        result = api.wait_for_event(awf_project, timeout=0)

        assert result.run_active is False


class TestMcpAnswerCarriesTheMode:
    """The field must survive the whole chain: WaitEventResult → as_dict →
    MCP answer. The tool-level tests stub ``as_dict``, so a regression in
    the real serialisation (a dropped field, a renamed key) would pass them.
    """

    def test_run_answer_and_advice_are_in_run_mode(self, awf_project, monkeypatch):
        from agent_workflow_ui.tools import awf as tools

        _live_single_start(awf_project, monkeypatch)
        run_state.write_run(
            awf_project,
            active=True,
            queue=["TODO-0001"],
            current="TODO-0001",
        )

        result = asyncio.run(
            tools.awf_wait_for_event(project_dir=str(awf_project), timeout=1)
        )

        assert result["status"] == "ok"
        assert result["run_active"] is True
        assert "DO NOT call awf_wait_for_event again" not in result["next_action"]

    def test_single_start_answer_is_reactive(self, awf_project, monkeypatch):
        from agent_workflow_ui.tools import awf as tools

        _live_single_start(awf_project, monkeypatch)

        result = asyncio.run(
            tools.awf_wait_for_event(project_dir=str(awf_project), timeout=1)
        )

        assert result["status"] == "ok"
        assert result["run_active"] is False


class TestWaitingPhraseInRunMode:
    """AC22: the phrase must be the same in both modes, not only the
    reactive one — a mode-dependent advice would drift between surfaces."""

    def test_status_and_brief_agree_in_a_run(self, awf_project, monkeypatch):
        from agent_workflow_ui.tools import awf as tools

        _running_project(awf_project, monkeypatch)
        run_state.write_run(
            awf_project,
            active=True,
            queue=["TODO-0001"],
            current="TODO-0001",
        )

        status = asyncio.run(tools.awf_status(str(awf_project)))
        brief = asyncio.run(tools.awf_brief(str(awf_project)))

        assert status["next_action"] == brief["next_action"]
        assert "wait only inside an active run" in status["next_action"]


class TestWaitingPhraseFitsTheCard:
    """The card clips ``next_action`` at 26 words: a phrase that does not
    fit loses its tail in the card while ``awf_status`` keeps it — the
    exact divergence IMP6 closes (23 words now, so the guard is real)."""

    def test_every_shipped_stage_name_keeps_the_phrase_unclipped(self):
        from pathlib import Path

        import yaml

        import awf
        from awf.brief import _NEXT_ACTION_MAX_WORDS, running_wait_advice

        root = Path(awf.__file__).parent / "templates" / "pipelines"
        names: set[str] = set()
        for path in root.glob("*.yaml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for stage in data.get("stages") or []:
                if isinstance(stage, dict) and stage.get("name"):
                    names.add(str(stage["name"]))
        assert names

        for name in sorted(names):
            advice = running_wait_advice(name)
            assert "wait only inside an active run" in advice
            assert "awf_wait_for_event" in advice
            assert len(advice.split()) <= _NEXT_ACTION_MAX_WORDS, name
