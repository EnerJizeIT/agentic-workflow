"""Tests for BD-10 pipeline context injection in build_prompt.

Validates that execute-stage workers get pipeline context telling them:
- Position in pipeline (stage N of M)
- What stages come before/after
- Their scope boundary (don't do others' work)
- Prior completed TODOs from done/
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf import api
from awf.supervisor import _build_pipeline_context, build_prompt


@pytest.fixture
def project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Test")
    pipes = tmp_git_repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        "stages:\n"
        '  - name: plan\n    role: supervisor\n'
        '  - name: agent-analyst\n    role: analyst\n'
        '  - name: agent-implementer\n    role: implementer\n'
        '  - name: verify\n    role: supervisor\n',
        encoding="utf-8",
    )
    return tmp_git_repo


def _write_state(project: Path, stage_name: str, stage_idx: int) -> None:
    state_dir = project / ".agentic" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "current.yaml").write_text(
        yaml.dump({"stage_name": stage_name, "stage_idx": stage_idx}),
        encoding="utf-8",
    )


class TestBuildPipelineContext:
    """BD-10: _build_pipeline_context generates role-aware context."""

    def test_implementer_context(self, project):
        _write_state(project, "agent-implementer", 2)
        ctx = _build_pipeline_context(project, "TODO-0002")
        assert "stage 3 of 4" in ctx
        assert "plan" in ctx and "agent-analyst" in ctx
        assert "verify" in ctx
        assert "implementation" in ctx.lower()

    def test_analyst_context(self, project):
        _write_state(project, "agent-analyst", 1)
        ctx = _build_pipeline_context(project, "TODO-0002")
        assert "stage 2 of 4" in ctx
        assert "plan" in ctx
        assert "agent-implementer" in ctx and "verify" in ctx
        assert "requirements analysis" in ctx.lower()

    def test_shows_completed_todos(self, project):
        _write_state(project, "agent-implementer", 2)
        done = project / ".agentic" / "done" / "TODO-0001"
        done.mkdir(parents=True)
        ctx = _build_pipeline_context(project, "TODO-0002")
        assert "TODO-0001" in ctx
        assert "don't redo" in ctx.lower()

    def test_no_completed_todos(self, project):
        _write_state(project, "agent-analyst", 1)
        ctx = _build_pipeline_context(project, "TODO-0001")
        assert "Prior completed" not in ctx

    def test_empty_without_state(self, project):
        ctx = _build_pipeline_context(project, "TODO-0001")
        assert ctx == ""

    def test_empty_without_pipeline(self, tmp_git_repo):
        api.init_project(tmp_git_repo, project_name="Test")
        ctx = _build_pipeline_context(tmp_git_repo, "TODO-0001")
        assert ctx == ""


class TestBuildPromptPipelineContext:
    """build_prompt injects pipeline context for execute stages."""

    def test_execute_prompt_has_pipeline_context(self, project):
        _write_state(project, "agent-implementer", 2)
        prompt = build_prompt("execute", "TODO-0002", project_dir=project)
        assert "Pipeline context" in prompt
        assert "stage 3 of 4" in prompt
        assert "implementation" in prompt.lower()

    def test_execute_prompt_without_project_dir(self):
        """No project_dir → no pipeline context (backward compat)."""
        prompt = build_prompt("execute", "TODO-0001")
        assert "Pipeline context" not in prompt
        assert "DONE-TODO-0001.ready" in prompt  # signal contract still there
