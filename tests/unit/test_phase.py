"""Tests for SMO phase detection, prompt assembly, and transitions."""
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
        "  - name: plan\n    role: supervisor\n"
        "  - name: worker\n    role: worker\n"
        "  - name: verify\n    role: supervisor\n"
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


class TestDetectPhaseRunActive:
    """RUN10 #1 (bug 2026-09-24): an active run (забег) IS the phase.

    The feedback header read "done" while the run was at position 4/6 —
    the clean-exit leftover phase=done won over the active run. The run
    check goes first, before the explicit-phase, stage-kind and
    goal/live branches. Without a run the detection is unchanged."""

    @staticmethod
    def _active_run(project: Path) -> None:
        from awf import run_state

        run_state.write_run(
            project,
            queue=["TODO-0001", "TODO-0002"],
            index=1,
            current="TODO-0002",
            active=True,
        )

    def test_active_run_wins_over_done_leftover(self, tmp_path):
        """(в) The bug case: clean-exit leftover phase=done + active run
        → 'run', not 'done'."""
        project = _setup_project(tmp_path)
        write_state(project, phase="done", goal="g", normalized=True)
        self._active_run(project)
        assert phase.detect_phase(project) == "run"

    def test_active_run_without_state(self, tmp_path):
        """Active run, no state file at all → 'run' (before the
        goal/setup branches)."""
        project = _setup_project(tmp_path)
        self._active_run(project)
        assert phase.detect_phase(project) == "run"

    def test_active_run_over_stage_kind(self, tmp_path):
        """Active run + live execute markers → 'run' (the run loop owns
        the cycle, including its verify step)."""
        project = _setup_project(tmp_path)
        write_state(
            project,
            stage_name="agent-impl",
            stage_kind="execute",
            stage_idx=2,
            todo_id="TODO-0002",
        )
        self._active_run(project)
        assert phase.detect_phase(project) == "run"

    def test_inactive_run_does_not_force_run(self, tmp_path):
        """A FINISHED run (active: false) must not pin the phase to
        'run' — the previous detection applies."""
        from awf import run_state

        project = _setup_project(tmp_path)
        run_state.write_run(project, queue=["TODO-0001"], index=1, active=False)
        assert phase.detect_phase(project) == "goal"

    def test_no_run_behavior_unchanged(self, tmp_path):
        """No run state: the explicit phase still wins (pre-RUN10 case)."""
        project = _setup_project(tmp_path)
        write_state(project, phase="done", goal="g", normalized=True)
        assert phase.detect_phase(project) == "done"


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

    def test_fallback_to_package_templates(self, tmp_path):
        """Without project-level templates, falls back to awf package
        templates."""
        project = tmp_path / "noTemplates"
        (project / ".agentic").mkdir(parents=True)
        prompt = phase.get_phase_prompt("goal", project)
        # Should still return the prompt (from awf templates)
        assert isinstance(prompt, str)
        assert "No prompt template found" not in prompt

    def test_missing_templates_degrade_message(self, tmp_path, monkeypatch):
        """AUD16-11: no templates anywhere (corrupted install) → honest
        degradation message. The old fallback to the full supervisor.md is
        gone — it was unreachable on an intact install and useless on a
        broken one."""
        import awf

        project = tmp_path / "noTemplates"
        (project / ".agentic").mkdir(parents=True)
        monkeypatch.setattr(
            awf, "__file__", str(tmp_path / "gone" / "awf" / "__init__.py"),
        )
        prompt = phase.get_phase_prompt("goal", project)
        assert "No prompt template found" in prompt


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


class TestAdvancePhaseFromPhase:
    """AUD02-05 (r4 2/3): a setup tool must make exactly its documented
    transition or refuse. Before the fix, awf_set_goal called mid-run
    (fresh state, stage_kind=execute) silently flipped phase to 'verify',
    and at phase='brief' it returned 'run' although the docstring promises
    'goal → form'."""

    def test_set_goal_mid_run_refused(self, tmp_path):
        from awf.api._errors import AwfApiError

        project = _setup_project(tmp_path)
        write_state(project, stage_kind="execute", goal="test")  # detect → 'run'
        with pytest.raises(AwfApiError) as exc:
            phase.advance_phase(project, from_phase="goal", goal="new goal")
        # The error names the current phase — a weak model can recover.
        assert "run" in str(exc.value)
        # And the phase key is NOT corrupted:
        state = read_state(project)
        assert state.get("phase") is None

    def test_set_goal_from_brief_refused(self, tmp_path):
        from awf.api._errors import AwfApiError

        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="brief", normalized=True)
        _add_pipeline(project)
        with pytest.raises(AwfApiError):
            phase.advance_phase(project, from_phase="goal", goal="x")
        assert read_state(project).get("phase") == "brief"  # untouched

    def test_goal_to_form_with_from_phase(self, tmp_path):
        project = _setup_project(tmp_path)
        new_phase = phase.advance_phase(project, from_phase="goal", goal="f")
        assert new_phase == "form"

    def test_normalize_to_brief_with_from_phase(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, goal="test", phase="normalize", normalized=True)
        _add_pipeline(project)
        new_phase = phase.advance_phase(
            project, from_phase="normalize", normalized=True
        )
        assert new_phase == "brief"


class TestStaleStateAfterFullCycle:
    """AUD02-04 (r4) + AUD02-12: the phase of a finished project must not
    degrade with time (and a live PID outranks a stale timestamp)."""

    def _age_state(self, project: Path, hours: float = 3.0) -> None:
        from datetime import datetime, timedelta, timezone

        import yaml

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        state_path = project / ".agentic" / "state" / "current.yaml"
        data = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        data["updated_at"] = old_ts
        state_path.write_text(yaml.safe_dump(data), encoding="utf-8")

    def test_full_cycle_aged_not_goal_or_normalize(self, tmp_path):
        """After the clean exit (phase=done + goal + normalized preserved by
        FU-13), >2h of age must NOT degrade detection to the setup phases."""
        project = _setup_project(tmp_path)
        write_state(project, phase="done", goal="test", normalized=True)
        _add_pipeline(project)
        self._age_state(project)
        result = phase.detect_phase(project)
        assert result not in ("init", "goal", "form", "normalize")

    def test_full_cycle_fresh_is_done(self, tmp_path):
        project = _setup_project(tmp_path)
        write_state(project, phase="done", goal="test", normalized=True)
        _add_pipeline(project)
        assert phase.detect_phase(project) == "done"

    def test_stale_state_live_pid_uses_stage_kind(self, tmp_path, monkeypatch):
        """AUD02-12: one stage longer than the 2h staleness window — with a
        live pipeline PID the stage must not disappear from detection."""
        import os

        import awf.api._liveness as liveness

        project = _setup_project(tmp_path)
        write_state(
            project,
            stage_kind="verify",
            goal="test",
            pipeline_pid=os.getpid(),
        )
        self._age_state(project)
        # The test process is our 'live pipeline' (patched cmdline identity):
        monkeypatch.setattr(
            liveness, "read_cmdline",
            lambda pid: "python\x00-m\x00awf\x00start\x00" if pid == os.getpid() else None,
        )
        assert phase.detect_phase(project) == "verify"

    def test_stale_state_dead_pid_falls_through(self, tmp_path):
        """Crash recovery unchanged: stale state + dead PID → detection by
        project state (as before the fix)."""
        project = _setup_project(tmp_path)
        write_state(project, phase="run", goal="test", pipeline_pid=999999999)
        self._age_state(project)
        # Stale + dead → file-based: goal set, no pipeline → 'form'
        assert phase.detect_phase(project) == "form"
