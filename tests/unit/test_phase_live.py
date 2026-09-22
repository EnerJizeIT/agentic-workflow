"""RUN3-7 (TODO-0049): live-project detection in the phase flow.

A missing goal is a setup signal only for a NEW project. A live project
(configured + completed work) stays in the working cycle even with a
wiped/empty state — otherwise a configured project with finished TODOs
would be sent through goal → form → normalize on every awf_current_step
call (the topic-trainer case: 19 finished tasks, phase 'goal').

Kept in a separate file so tests/unit/test_phase.py stays under the
files_over_400 ratchet.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import phase
from awf.pipeline_state import read_state, write_state


def _setup_project(tmp_path: Path) -> Path:
    """Create a project with .agentic/ structure."""
    project = tmp_path / "proj"
    (project / ".agentic").mkdir(parents=True)
    (project / ".agentic" / "state").mkdir(parents=True)
    return project


def _add_pipeline(project: Path) -> None:
    """Add a configured pipeline."""
    pipes = project / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "name: default\nstages:\n"
        "  - name: plan\n    role: supervisor\n    kind: plan\n"
        "  - name: worker\n    role: worker\n    kind: execute\n"
        "  - name: verify\n    role: supervisor\n    kind: verify\n"
    )


def _add_done(project: Path, todo_id: str = "TODO-0001") -> None:
    """Add a completed-TODO archive under .agentic/done/."""
    done = project / ".agentic" / "done" / todo_id
    done.mkdir(parents=True, exist_ok=True)
    (done / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")


def _add_config_with_roles(project: Path) -> None:
    """Add a config.yaml with at least one role under ``models:``."""
    (project / ".agentic" / "config.yaml").write_text(
        "models:\n  worker:\n    model: test/model\n", encoding="utf-8"
    )


class TestLiveProjectGoal:
    """Phase detection: new vs live project without a stored goal."""

    def test_new_project_no_goal_is_goal(self, tmp_path):
        """No pipeline, no roles in config, empty done/ → goal (as before)."""
        project = _setup_project(tmp_path)
        assert phase.detect_phase(project) == "goal"

    def test_live_pipeline_no_goal_is_run(self, tmp_path):
        """Pipeline + completed TODO, no goal in state → run (was goal)."""
        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_done(project)
        assert phase.detect_phase(project) == "run"

    def test_live_config_roles_no_pipeline_no_goal_is_run(self, tmp_path):
        """config.yaml roles + completed TODO (no pipeline file) → run."""
        project = _setup_project(tmp_path)
        _add_config_with_roles(project)
        _add_done(project)
        assert phase.detect_phase(project) == "run"

    def test_configured_but_never_run_stays_goal(self, tmp_path):
        """Pipeline + roles but empty done/ → still 'goal': no work
        evidence, the setup chain applies (pinned boundary)."""
        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_config_with_roles(project)
        assert phase.detect_phase(project) == "goal"

    def test_done_without_config_stays_goal(self, tmp_path):
        """Archives in done/ but no config.yaml file and no pipeline →
        'goal': work evidence without a configured team (pinned)."""
        project = _setup_project(tmp_path)
        _add_done(project)
        assert phase.detect_phase(project) == "goal"

    def test_done_with_init_stub_config_stays_goal(self, tmp_path):
        """The init stub (``models: {supervisor: {description: ...}}``)
        is written by awf_init into EVERY new project — it is not a
        configured team. Archived work with no team and no pipeline
        still goes through the setup chain (pinned)."""
        project = _setup_project(tmp_path)
        (project / ".agentic" / "config.yaml").write_text(
            "models:\n  supervisor:\n    description: Current session model\n",
            encoding="utf-8",
        )
        _add_done(project)
        assert phase.detect_phase(project) == "goal"

    def test_live_with_goal_follows_old_chain(self, tmp_path):
        """Goal present → old chain untouched: pipeline set, not
        normalized → normalize, regardless of done/."""
        project = _setup_project(tmp_path)
        write_state(project, goal="test")
        _add_pipeline(project)
        _add_done(project)
        assert phase.detect_phase(project) == "normalize"

    def test_live_stale_state_is_run(self, tmp_path):
        """Stale state (>2h) without goal + pipeline + done → run: the
        wiped-state case must not fall back to 'goal'."""
        from datetime import datetime, timedelta, timezone

        import yaml

        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_done(project)
        write_state(project, stage_kind="execute")
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        state_path = project / ".agentic" / "state" / "current.yaml"
        data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        data["updated_at"] = old_ts
        state_path.write_text(yaml.safe_dump(data), encoding="utf-8")
        assert phase.detect_phase(project) == "run"


class TestLiveProjectPrompt:
    """The run prompt of a live project without a stored goal gets ONE
    explanatory line — no template rewrite."""

    def test_run_prompt_has_setup_note_when_live_no_goal(self, tmp_path):
        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_done(project)
        prompt = phase.get_phase_prompt("run", project)
        assert "setup chain" in prompt
        assert "not needed" in prompt

    def test_run_prompt_no_note_for_new_project(self, tmp_path):
        project = _setup_project(tmp_path)
        prompt = phase.get_phase_prompt("run", project)
        assert "setup chain" not in prompt

    def test_run_prompt_no_note_when_goal_stored(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="Build X")
        _add_pipeline(project)
        _add_done(project)
        prompt = phase.get_phase_prompt("run", project)
        assert "Build X" in prompt
        assert "setup chain" not in prompt


class TestLiveProjectTransitions:
    """SMO setup tools keep their contract — the documented transition
    works on a new project, and is refused on a live one (a live project
    without a goal is detected as 'run')."""

    def test_set_goal_still_works_on_new_project(self, tmp_path):
        project = _setup_project(tmp_path)
        new_phase = phase.advance_phase(project, from_phase="goal", goal="f")
        assert new_phase == "form"

    def test_confirm_normalized_still_works_on_new_project(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="normalize")
        _add_pipeline(project)
        new_phase = phase.advance_phase(
            project, from_phase="normalize", normalized=True
        )
        assert new_phase == "brief"

    def test_set_goal_refused_on_live_project(self, tmp_path):
        from awf.api._errors import AwfApiError

        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_done(project)
        with pytest.raises(AwfApiError) as exc:
            phase.advance_phase(project, from_phase="goal", goal="f")
        # The error names the actual phase — a weak model can recover.
        assert "run" in str(exc.value)
        # And no setup chain was entered on a live project:
        state = read_state(project)
        assert state is None or state.get("goal") is None

    def test_confirm_normalized_refused_on_live_project(self, tmp_path):
        from awf.api._errors import AwfApiError

        project = _setup_project(tmp_path)
        _add_pipeline(project)
        _add_done(project)
        with pytest.raises(AwfApiError):
            phase.advance_phase(
                project, from_phase="normalize", normalized=True
            )
        state = read_state(project)
        assert state is None or state.get("normalized") is None
