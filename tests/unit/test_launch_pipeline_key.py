"""A12b/A12c (incident 31.10): the launch honours the unit, not the shadow.

Three guarantees pinned here:

1. ``run_pipeline`` reads the TODO's own ``pipeline:`` key when no flag is
   given — ``awf start`` used to fall back to the project default, so a spec
   unit launched the dev pipeline.
2. A unit's frozen snapshot refuses an EXPLICIT request for another
   pipeline instead of silently running the old one (the operator saw the
   spec file's name in the banner and the dev stages in the list).
3. A pinned, already-dispatched unit skips the plan stage's supervisor
   subprocess — it renumbered the unit and moved the original file.
"""
from __future__ import annotations

from pathlib import Path

from awf import orchestrator, pipeline_engine
from awf.pipeline import Stage, pipeline_snapshot_text


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "pipelines"):
        (project / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    return project


def _todo(project: Path, todo_id: str = "TODO-0001", contract: str = "") -> Path:
    path = project / ".agentic" / "inbox" / f"{todo_id}.md"
    path.write_text(f"---\n{contract}---\n# TODO — x\n", encoding="utf-8")
    (path.parent / f"{todo_id}.ready").write_text("", encoding="utf-8")
    return path


# ─── (1) the TODO's pipeline key ────────────────────────────────────────


def test_declared_pipeline_is_read_from_the_todo(tmp_path):
    project = _project(tmp_path)
    _todo(project, contract="pipeline: spec-opencode2only\n")
    assert (
        orchestrator._declared_pipeline_name(project, "TODO-0001")
        == "spec-opencode2only"
    )


def test_declared_pipeline_absent_is_the_project_default(tmp_path):
    project = _project(tmp_path)
    _todo(project)
    assert orchestrator._declared_pipeline_name(project, "TODO-0001") == ""


def test_declared_pipeline_of_a_missing_todo_is_empty(tmp_path):
    project = _project(tmp_path)
    assert orchestrator._declared_pipeline_name(project, "TODO-0999") == ""


def test_declared_pipeline_falls_back_to_the_newest_active_todo(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _todo(project, "TODO-0002", contract="pipeline: spec\n")
    monkeypatch.setattr(
        "awf.todos.newest_active", lambda project_dir: "TODO-0002"
    )
    assert orchestrator._declared_pipeline_name(project, "") == "spec"


# ─── (2) a snapshot of another pipeline ─────────────────────────────────


def _snapshot(project: Path, todo_id: str, name: str) -> Path:
    path = project / ".agentic" / "context" / f"PIPELINE-{todo_id}.yaml"
    stages = [Stage(name="plan", role="supervisor", kind="plan")]
    path.write_text(pipeline_snapshot_text(stages, name, todo_id), encoding="utf-8")
    return path


def _live(project: Path, name: str) -> Path:
    path = project / ".agentic" / "pipelines" / f"{name}.yaml"
    path.write_text("name: x\nstages:\n- name: plan\n  role: supervisor\n")
    return path


def test_snapshot_of_another_pipeline_refuses_the_launch(tmp_path, capsys):
    project = _project(tmp_path)
    _todo(project)
    _snapshot(project, "TODO-0001", "default")
    live = _live(project, "spec")

    result = orchestrator._apply_pipeline_snapshot(
        project, [Stage(name="plan", role="supervisor", kind="plan")], live,
        "TODO-0001", False, project / ".agentic" / "logs",
    )

    assert result is None
    err = capsys.readouterr().err
    assert "pinned to pipeline 'default'" in err
    assert "asked for 'spec'" in err


def test_snapshot_of_the_same_pipeline_still_wins(tmp_path):
    project = _project(tmp_path)
    _todo(project)
    _snapshot(project, "TODO-0001", "spec")
    live = _live(project, "spec")

    result = orchestrator._apply_pipeline_snapshot(
        project, [Stage(name="other", role="x", kind="execute")], live,
        "TODO-0001", False, project / ".agentic" / "logs",
    )

    assert result is not None
    assert [s.name for s in result] == ["plan"]


# ─── (3) the plan stage does not rewrite a pinned unit ──────────────────


def test_pinned_plan_skips_the_supervisor_subprocess(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _todo(project, "TODO-0001")
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        raise AssertionError("the supervisor must not run for a pinned unit")

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", boom)
    monkeypatch.setattr(pipeline_engine, "_run_plan_checkpoint_gate",
                        lambda *a, **kw: 0)
    monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

    todo, delta, rc = pipeline_engine.execute_supervisor_stage(
        stage=Stage(name="plan", role="supervisor", kind="plan"),
        current_todo="TODO-0001",
        auto=True,
        project_dir=project,
        config={},
        logs_dir=project / ".agentic" / "logs",
    )

    assert calls["n"] == 0
    assert (todo, delta, rc) == ("TODO-0001", 1, 0)


def test_unpinned_plan_still_asks_the_supervisor(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _todo(project, "TODO-0001")
    calls = {"n": 0}

    def record(*args, **kwargs):
        calls["n"] += 1
        return ""

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", record)
    monkeypatch.setattr(pipeline_engine, "_run_plan_checkpoint_gate",
                        lambda *a, **kw: 0)
    monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)

    todo, delta, rc = pipeline_engine.execute_supervisor_stage(
        stage=Stage(name="plan", role="supervisor", kind="plan"),
        current_todo="",
        auto=True,
        project_dir=project,
        config={},
        logs_dir=project / ".agentic" / "logs",
    )

    assert calls["n"] == 1
    assert (todo, delta, rc) == ("TODO-0001", 1, 0)
