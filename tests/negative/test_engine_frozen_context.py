"""CTX1: the worker prompt reads the unit's frozen pipeline snapshot.

The engine executes ``context/PIPELINE-<id>.yaml`` captured at launch, but
``_build_pipeline_context`` used to parse the live ``default.yaml``. A
template edited mid-run then leaked new stages into the prompt the worker
read (TODO-0270 audit: prompt said 7 stages, the engine ran 6).
"""
from __future__ import annotations

import yaml

from awf import api
from awf.pipeline_state import write_state
from awf.supervisor import _build_pipeline_context, build_prompt

LIVE_PIPELINE = """\
stages:
  - name: plan
    role: supervisor
  - name: agent-implementer
    role: implementer
  - name: agent-architect-review
    role: architect
  - name: verify
    role: supervisor
"""

SNAPSHOT_PIPELINE = """\
name: default
stages:
  - name: plan
    role: supervisor
  - name: agent-implementer
    role: implementer
  - name: verify
    role: supervisor
"""


def _project(tmp_git_repo):
    api.init_project(tmp_git_repo, project_name="Frozen")
    pipes = tmp_git_repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(LIVE_PIPELINE, encoding="utf-8")
    return tmp_git_repo


def _write_snapshot(project, name: str, text: str) -> None:
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"PIPELINE-{name}.yaml").write_text(text, encoding="utf-8")


class TestFrozenPipelineContext:
    def test_prompt_describes_snapshot_not_live_template(self, tmp_git_repo):
        project = _project(tmp_git_repo)
        _write_snapshot(project, "TODO-0001", SNAPSHOT_PIPELINE)
        write_state(project, stage_name="agent-implementer", todo_id="TODO-0001")

        ctx = _build_pipeline_context(project, "TODO-0001")

        assert "stage 2 of 3" in ctx, ctx
        assert "After you: verify" in ctx, ctx
        assert "agent-architect-review" not in ctx, ctx

    def test_mid_run_template_edit_does_not_leak_new_stages(self, tmp_git_repo):
        project = _project(tmp_git_repo)
        _write_snapshot(project, "TODO-0001", SNAPSHOT_PIPELINE)
        write_state(project, stage_name="agent-implementer", todo_id="TODO-0001")
        # The owner edits the live template while the unit is running.
        (project / ".agentic" / "pipelines" / "default.yaml").write_text(
            LIVE_PIPELINE + "  - name: agent-added-mid-run\n    role: specialist\n",
            encoding="utf-8",
        )

        prompt = build_prompt("execute", "TODO-0001", project_dir=project)

        assert "stage 2 of 3" in prompt
        assert "agent-added-mid-run" not in prompt
        assert "agent-architect-review" not in prompt

    def test_live_template_is_the_fallback_without_snapshot(self, tmp_git_repo):
        project = _project(tmp_git_repo)
        write_state(project, stage_name="agent-implementer", todo_id="TODO-0001")

        ctx = _build_pipeline_context(project, "TODO-0001")

        assert "stage 2 of 4" in ctx, ctx
        assert "After you: agent-architect-review, verify" in ctx, ctx

    def test_malformed_snapshot_degrades_to_live(self, tmp_git_repo):
        project = _project(tmp_git_repo)
        _write_snapshot(
            project,
            "TODO-0001",
            yaml.safe_dump(
                {"stages": [{"name": "plan", "role": "supervisor", "bogus": 1}]}
            ),
        )
        write_state(project, stage_name="agent-implementer", todo_id="TODO-0001")

        ctx = _build_pipeline_context(project, "TODO-0001")

        assert "stage 2 of 4" in ctx, ctx
