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


class TestPriorCompletedTodos:
    """R1/AC1–AC4 (IMP1): the last eight units, never the whole archive."""

    @staticmethod
    def _done(project: Path, names: list[str]) -> None:
        done = project / ".agentic" / "done"
        done.mkdir(parents=True, exist_ok=True)
        for name in names:
            (done / name).mkdir()

    @staticmethod
    def _fragment(ctx: str) -> str:
        """The completed-units fragment: from its line to the end."""
        return ctx[ctx.index("Prior completed TODOs:"):]

    def test_last_eight_by_number(self, project):
        _write_state(project, "agent-implementer", 2)
        self._done(project, [f"TODO-{n:04d}" for n in range(1, 13)])

        ctx = _build_pipeline_context(project, "TODO-0013")
        fragment = self._fragment(ctx)

        assert "TODO-0004" not in fragment
        assert "TODO-0001" not in fragment
        listed = fragment.split("Prior completed TODOs: ")[1].split("\n")[0]
        # K1/T1: newest first, exactly eight — the line, not just the set.
        expected = ", ".join(f"TODO-{n:04d}" for n in range(12, 4, -1))
        assert listed == expected

    def test_current_unit_is_not_listed(self, project):
        _write_state(project, "agent-implementer", 2)
        self._done(project, ["TODO-0001", "TODO-0002", "TODO-0003"])

        fragment = self._fragment(_build_pipeline_context(project, "TODO-0003"))

        assert "TODO-0002" in fragment
        assert "TODO-0003" not in fragment

    def test_order_is_the_number_not_the_name(self, project):
        _write_state(project, "agent-implementer", 2)
        self._done(project, ["TODO-9", "TODO-10", "TODO-10000"])

        line = self._fragment(_build_pipeline_context(project, "TODO-20000"))
        line = line.split("\n")[0]

        # K1: descending by number, so TODO-9 sorts AFTER TODO-10 (name
        # sorting would put it first) and TODO-10000 comes first.
        assert line.index("TODO-10000") < line.index("TODO-10,") < line.index("TODO-9")

    def test_hint_names_the_report_and_the_archive(self, project):
        _write_state(project, "agent-implementer", 2)
        self._done(project, ["TODO-0001"])

        ctx = _build_pipeline_context(project, "TODO-0002")

        assert "awf report" in ctx
        assert ".agentic/done/" in ctx

    def test_fragment_does_not_grow_with_the_archive(self, project):
        """CTX5: 297 ids were 3 288 characters of a 5 654-character prompt."""
        _write_state(project, "agent-implementer", 2)
        names = [f"TODO-{n:04d}" for n in range(1, 1001)]
        self._done(project, names)
        big = self._fragment(_build_pipeline_context(project, "TODO-9000"))

        for name in names[:-8]:
            (project / ".agentic" / "done" / name).rmdir()
        small = self._fragment(_build_pipeline_context(project, "TODO-9000"))

        assert big == small
        assert len(big) <= 300

    def test_the_other_context_blocks_are_unchanged(self, project):
        _write_state(project, "agent-implementer", 2)
        self._done(project, ["TODO-0001"])

        ctx = _build_pipeline_context(project, "TODO-0002")

        assert "## Pipeline context" in ctx
        assert "Before you:" in ctx
        assert "After you:" in ctx
        assert "Your scope: implementation" in ctx


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
