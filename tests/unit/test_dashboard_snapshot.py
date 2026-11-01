"""A20: the dashboard draws the pipeline that RUNS, not the one published.

A pinned unit executes its snapshot (``context/PIPELINE-<id>.yaml``); the
published file may have been edited after the unit started. Drawing the
published stages showed a list the engine was not executing — and the
highlight followed that list.
"""
from __future__ import annotations

from pathlib import Path

from awf.api.dashboard import generate_state_dict
from awf.pipeline import Stage, pipeline_snapshot_text
from awf.pipeline_state import write_state


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "pipelines"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: Dashboard\n", encoding="utf-8"
    )
    (project / ".agentic" / "pipelines" / "default.yaml").write_text(
        "name: default\nstages:\n"
        "- name: plan\n  role: supervisor\n"
        "- name: implement\n  role: worker\n"
        "- name: verify\n  role: supervisor\n",
        encoding="utf-8",
    )
    return project


def _snapshot(project: Path, todo: str = "TODO-0001") -> None:
    stages = [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="spec-context", role="agent-project-auditor", kind="execute"),
        Stage(name="verify", role="supervisor", kind="verify"),
    ]
    (project / ".agentic" / "context" / f"PIPELINE-{todo}.yaml").write_text(
        pipeline_snapshot_text(stages, "spec-opencode2only", todo), encoding="utf-8"
    )


def test_the_snapshot_stages_win(tmp_path):
    project = _project(tmp_path)
    _snapshot(project)
    write_state(project, todo_id="TODO-0001", pipeline="spec-opencode2only", stage_idx=1)

    state = generate_state_dict(project)

    names = [s["name"] for s in state["stages"]]
    assert names == ["plan", "spec-context", "verify"]
    assert "снимку" in state["pipeline_note"]


def test_without_a_snapshot_the_published_file_is_drawn(tmp_path):
    project = _project(tmp_path)
    write_state(project, todo_id="TODO-0001", pipeline="default", stage_idx=1)

    state = generate_state_dict(project)

    names = [s["name"] for s in state["stages"]]
    assert names == ["plan", "implement", "verify"]
    assert state["pipeline_note"] == ""
