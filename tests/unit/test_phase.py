"""Tests for SMO phase detection, prompt assembly, and transitions."""
from __future__ import annotations

from pathlib import Path

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


class TestDetectPhase:
    """Phase detection from project state."""

    def test_init_no_agentic(self, tmp_path):
        """No .agentic/ → init phase."""
        assert phase.detect_phase(tmp_path / "empty") == "init"

    def test_goal_no_goal_in_state(self, tmp_path):
        """.agentic/ exists, no goal → goal phase."""
        project = _setup_project(tmp_path)
        assert phase.detect_phase(project) == "goal"

    def test_form_goal_set_no_pipeline(self, tmp_path):
        """Goal set, no pipeline → form phase."""
        project = _setup_project(tmp_path)
        write_state(project, goal="test feature")
        assert phase.detect_phase(project) == "form"

    def test_normalize_pipeline_no_normalize(self, tmp_path):
        """Pipeline configured, not normalized → normalize phase."""
        project = _setup_project(tmp_path)
        write_state(project, goal="test feature")
        _add_pipeline(project)
        assert phase.detect_phase(project) == "normalize"

    def test_brief_normalized_no_todo(self, tmp_path):
        """Normalized, no active TODO → brief phase."""
        project = _setup_project(tmp_path)
        write_state(project, goal="test feature", normalized=True)
        _add_pipeline(project)
        assert phase.detect_phase(project) == "brief"

    def test_explicit_phase_in_state(self, tmp_path):
        """Explicit phase in state overrides detection."""
        project = _setup_project(tmp_path)
        write_state(project, phase="run", goal="test")
        assert phase.detect_phase(project) == "run"

    def test_stale_state_falls_through(self, tmp_path):
        """Stale state → detection by project state, not state phase."""
        from datetime import datetime, timedelta, timezone
        import yaml
        project = _setup_project(tmp_path)
        # Write stale state directly (write_state overwrites updated_at)
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        (project / ".agentic" / "state" / "current.yaml").write_text(
            yaml.safe_dump({"phase": "run", "goal": "test", "updated_at": old_ts})
        )
        # Stale state → detect by project state (goal set, no pipeline → form)
        result = phase.detect_phase(project)
        assert result == "form"

    def test_stage_kind_plan_when_no_explicit_phase(self, tmp_path):
        """State has stage_kind=plan but no phase → brief."""
        project = _setup_project(tmp_path)
        write_state(project, stage_kind="plan", goal="test", normalized=True)
        _add_pipeline(project)
        assert phase.detect_phase(project) == "brief"

    def test_stage_kind_execute(self, tmp_path):
        """State has stage_kind=execute but no phase → run."""
        project = _setup_project(tmp_path)
        write_state(project, stage_kind="execute", goal="test")
        assert phase.detect_phase(project) == "run"

    def test_stage_kind_verify(self, tmp_path):
        """State has stage_kind=verify but no phase → verify."""
        project = _setup_project(tmp_path)
        write_state(project, stage_kind="verify", goal="test")
        assert phase.detect_phase(project) == "verify"


class TestGetPhasePrompt:
    """Phase prompt assembly."""

    def test_returns_string(self, tmp_path):
        """get_phase_prompt returns non-empty string."""
        project = _setup_project(tmp_path)
        prompt = phase.get_phase_prompt("goal", project)
        assert isinstance(prompt, str)
        assert len(prompt) > 0

    def test_core_content_present(self, tmp_path):
        """Prompt contains core invariants."""
        project = _setup_project(tmp_path)
        prompt = phase.get_phase_prompt("init", project)
        assert "supervisor" in prompt.lower() or "Supervisor" in prompt

    def test_phase_content_present(self, tmp_path):
        """Prompt contains phase-specific instructions."""
        project = _setup_project(tmp_path)
        prompt = phase.get_phase_prompt("goal", project)
        assert "goal" in prompt.lower()

    def test_goal_injected(self, tmp_path):
        """Goal from state is injected into prompt."""
        project = _setup_project(tmp_path)
        write_state(project, goal="Build feature X")
        prompt = phase.get_phase_prompt("form", project)
        assert "Build feature X" in prompt

    def test_fallback_to_supervisor_md(self, tmp_path):
        """Without phase files, falls back to supervisor.md."""
        project = tmp_path / "noTemplates"
        (project / ".agentic").mkdir(parents=True)
        prompt = phase.get_phase_prompt("goal", project)
        # Should still return a string (from awf templates)
        assert isinstance(prompt, str)


class TestAdvancePhase:
    """Phase transitions."""

    def test_goal_to_form(self, tmp_path):
        project = _setup_project(tmp_path)
        # No goal in state → detect returns "goal"
        # advance_phase moves goal→form, storing the goal
        new_phase = phase.advance_phase(project, goal="test feature")
        assert new_phase == "form"

    def test_form_to_normalize(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="form")
        _add_pipeline(project)
        new_phase = phase.advance_phase(project)
        assert new_phase == "normalize"

    def test_normalize_to_brief(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="normalize", normalized=True)
        _add_pipeline(project)
        new_phase = phase.advance_phase(project, normalized=True)
        assert new_phase == "brief"

    def test_brief_to_run(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="brief", normalized=True)
        _add_pipeline(project)
        new_phase = phase.advance_phase(project)
        assert new_phase == "run"

    def test_advance_writes_state(self, tmp_path):
        """advance_phase writes new phase to state."""
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="goal")
        phase.advance_phase(project, goal="test")
        state = read_state(project)
        assert state is not None
        assert state.get("phase") == "form"

    def test_done_stays_done(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, phase="done", goal="test")
        new_phase = phase.advance_phase(project)
        assert new_phase == "done"
