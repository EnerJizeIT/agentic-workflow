"""TODO-0304: ``checklist-close`` renamed, ``checklist-check`` follow-up.

The close stage keeps the gate semantics; the new stage reviews ONLY the
closer's diff and skips on the recorded fact that the closer did not run.
Sessions: close continues the implementer, check continues QA.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from awf import agent_stage, closer_stage, pipeline_engine, session_store
from awf.api import setup
from awf.pipeline import Stage, load_stages

ROOT = Path(__file__).resolve().parents[2]
CHECK_TASK_MARKERS = (
    "ТОЛЬКО дифф checklist-close",
    "без полного круга",
    "## REVIEW-фикс",
)


def _repo_default() -> list[Stage]:
    source = ROOT / ".agentic" / "pipelines" / "default.yaml"
    if not source.is_file():
        pytest.skip("repo .agentic absent (gitignored in clean CI checkout)")
    return load_stages(source)


def test_default_pipeline_renames_close_and_adds_check():
    """R1/R2/R5: the local default carries both stages in order."""
    stages = _repo_default()
    names = [s.name for s in stages]
    assert names.index("checklist-close") < names.index("checklist-check")
    assert names.index("checklist-check") < names.index("agent-project-auditor")

    close = stages[names.index("checklist-close")]
    assert close.role == "agent-implementer"
    assert close.skip_if == "checklist_green"
    assert close.write_check == "off"
    assert close.session_key == "agent-implementer"

    check = stages[names.index("checklist-check")]
    assert check.role == "agent-qa-review"
    assert check.skip_if == "checklist_close_skipped"
    assert check.session_key == "agent-qa-review"
    for marker in CHECK_TASK_MARKERS:
        assert marker in check.task


@pytest.mark.parametrize("variant", ["full", "code-health"])
def test_templates_and_generator_carry_the_pair(variant):
    """R5: shipped templates and the form generator agree."""
    template = setup._builtin_pipeline_template(variant)
    stages = load_stages(template)
    names = [s.name for s in stages]
    assert names[names.index("checklist-close") + 1] == "checklist-check"

    generated = setup.build_pipeline_stages(
        [{"agent": "agent-implementer"}, {"agent": "agent-qa-review"}]
    )
    gen_names = [s["name"] for s in generated]
    assert gen_names.index("checklist-close") + 1 == gen_names.index(
        "checklist-check"
    )
    close = generated[gen_names.index("checklist-close")]
    check = generated[gen_names.index("checklist-check")]
    assert close["session_key"] == "agent-implementer"
    assert check["session_key"] == "agent-qa-review"
    assert check["skip_if"] == "checklist_close_skipped"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "context", "state", "roles"):
        (project / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project:\n  name: CheckStage\n", encoding="utf-8"
    )
    # Hermetic: the stage resolves its role from the project, never from the
    # machine's ~/.config/awf/roles (CI has none; a clean HOME reproduced the
    # failure where the role error was swallowed by pytest.raises(RuntimeError)).
    (project / ".agentic" / "roles" / "agent-qa-review.md").write_text(
        "# agent-qa-review\n", encoding="utf-8"
    )
    return project


def _followup_stage() -> Stage:
    return Stage(
        name="checklist-check",
        role="agent-qa-review",
        session_key="agent-qa-review",
        skip_if="checklist_close_skipped",
    )


def test_followup_runs_when_close_ran_and_skips_when_it_did_not(tmp_path):
    """R3: the fact decides — scoped to the unit, never the gate."""
    project = _project(tmp_path)
    stage = _followup_stage()

    # No record at all: the close did not run — nothing to review.
    assert closer_stage.decide(project, "TODO-0001", stage, {}).kind == "skip"

    # Another unit's fact never decides this unit.
    closer_stage.record_close_skipped(project, "TODO-0002", skipped=True)
    assert closer_stage.decide(project, "TODO-0001", stage, {}).kind == "skip"

    # Close ran for THIS unit: the check runs.
    closer_stage.record_close_skipped(project, "TODO-0001", skipped=False)
    decision = closer_stage.decide(project, "TODO-0001", stage, {})
    assert decision.kind == "run"


def test_followup_skip_reason_is_the_fact_not_the_gate(tmp_path):
    project = _project(tmp_path)
    closer_stage.record_close_skipped(project, "TODO-0001", skipped=True)
    decision = closer_stage.decide(
        project, "TODO-0001", _followup_stage(), {}
    )
    assert decision.kind == "skip"
    assert decision.skip_reason == "checklist_close_skipped"


def _git_project(tmp_path: Path) -> Path:
    import subprocess

    project = _project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / ".gitignore").write_text(".agentic/\n", encoding="utf-8")
    (project / "ok").mkdir()
    (project / "ok" / "a").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=project, check=True)
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO-0001\n\n## Чек-лист завершения\n\n"
        '- [ ] unit-01: unit-01 — probe(file_exists ["ok/a"]) expect present\n',
        encoding="utf-8",
    )
    return project


def _stages() -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor"),
        Stage(
            name="checklist-close",
            role="agent-implementer",
            session_key="agent-implementer",
            skip_if="checklist_green",
            write_check="off",
        ),
        _followup_stage(),
        Stage(name="verify", role="supervisor"),
    ]


def test_green_checklist_skips_close_and_check_without_a_spawn(
    tmp_path, monkeypatch
):
    """R3: a green checklist skips BOTH — no close, no check."""
    project = _git_project(tmp_path)
    spawns: list = []
    monkeypatch.setattr(
        pipeline_engine,
        "_run_agent_stage",
        lambda stage, todo_id, *a, **kw: spawns.append(stage.name),
    )
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    stages = _stages()

    todo, idx, rc = pipeline_engine.execute_agent_stage(
        stage=stages[1],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=project / ".agentic" / "logs",
        stages=stages,
        stage_idx=1,
        retry_counts=[0, 0, 0, 0],
        auto=False,
        agent_hard_timeout=None,
        pipeline_name=None,
    )
    assert (todo, idx, rc) == ("TODO-0001", 2, 0)
    assert spawns == []

    todo, idx, rc = pipeline_engine.execute_agent_stage(
        stage=stages[2],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=project / ".agentic" / "logs",
        stages=stages,
        stage_idx=2,
        retry_counts=[0, 0, 0, 0],
        auto=False,
        agent_hard_timeout=None,
        pipeline_name=None,
    )
    assert (todo, idx, rc) == ("TODO-0001", 3, 0)
    assert spawns == []
    state = pipeline_engine.read_state(project)
    assert state["stage_skip_reason"] == "checklist_close_skipped"


def _red_git_project(tmp_path: Path) -> Path:
    project = _git_project(tmp_path)
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO-0001\n\n## Чек-лист завершения\n\n"
        '- [ ] unit-01: unit-01 — probe(file_exists ["missing.txt"]) '
        "expect present\n",
        encoding="utf-8",
    )
    return project


def test_close_entry_records_the_fact_before_the_spawn(tmp_path, monkeypatch):
    """IMP5: a red checklist enters the close; the fact lands before spawn.

    A crash during the closer's spawn must still leave the "close ran"
    fact — it is the ONLY input of the follow-up's skip decision. Without
    this pin, a crash would silently turn a pending review into a skip.
    """
    project = _red_git_project(tmp_path)
    calls: list = []

    def boom(*args, **kwargs):
        calls.append("spawn")
        raise RuntimeError("crash before the worker run")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", boom)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    stages = _stages()

    todo, idx, rc = pipeline_engine.execute_agent_stage(
        stage=stages[1],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=project / ".agentic" / "logs",
        stages=stages,
        stage_idx=1,
        retry_counts=[0, 0, 0, 0],
        auto=False,
        agent_hard_timeout=None,
        pipeline_name=None,
    )

    assert calls == ["spawn"]
    # The crash stops the pipeline, but the fact was written first.
    assert rc == 1
    state = pipeline_engine.read_state(project)
    assert state["checklist_close_skipped"] is False
    assert state["checklist_close_skipped_todo"] == "TODO-0001"
    assert closer_stage.decide(project, "TODO-0001", stages[2], {}).kind == "run"


def _session_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "directory TEXT NOT NULL DEFAULT '', time_created INTEGER, "
        "time_updated INTEGER)"
    )
    con.commit()
    con.close()
    return path


def test_qa_role_session_is_reusable_for_the_check(tmp_path, monkeypatch):
    """R4: the check continues the QA pair file (SESSION-<todo>-<role>)."""
    project = _project(tmp_path)
    db = _session_db(tmp_path / "opencode.db")
    now_ms = int(time.time() * 1000)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?)",
        ("s-qa", "awf-agent-qa-review-agent-qa-review-TODO-0001-a1", "/p",
         now_ms, now_ms),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    pair = session_store.session_file(project, "TODO-0001", "agent-qa-review")
    fresh = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    pair.write_text(
        json.dumps(
            {
                "session_id": "s-qa",
                "title": "awf-agent-qa-review-agent-qa-review-TODO-0001-a1",
                "agent": "agent-qa-review",
                "model": "",
                "attempt": 1,
                "ts": fresh,
            }
        ),
        encoding="utf-8",
    )

    decision = session_store.decide(
        project,
        todo_id="TODO-0001",
        stage="agent-qa-review",
        role="agent-qa-review",
        agent="agent-qa-review",
        model="",
        attempt=2,
        config={"engine": {"reuse_session": "always"}},
    )
    assert decision.reused is True
    assert decision.session_id == "s-qa"


def test_session_key_round_trips_through_the_snapshot():
    from awf.pipeline import pipeline_snapshot_text

    stages = _stages()
    text = pipeline_snapshot_text(stages, "default", "TODO-0001")
    assert "session_key: agent-implementer" in text
    assert "session_key: agent-qa-review" in text


def test_agent_stage_uses_the_session_key(tmp_path, monkeypatch):
    """R4: the pair key is ``session_key``, not the stage address."""
    import awf.signal_watch as signal_watch

    project = _project(tmp_path)
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "# TODO-0001\n", encoding="utf-8"
    )
    stage = Stage(
        name="checklist-check",
        role="agent-qa-review",
        session_key="agent-qa-review",
    )

    def stop_before_spawn(*args, **kwargs):
        raise RuntimeError("stop after the decision")

    monkeypatch.setattr(
        signal_watch, "run_subprocess_until_signal", stop_before_spawn
    )
    with pytest.raises(RuntimeError):
        agent_stage.run_agent_stage(
            stage=stage, todo_id="TODO-0001", project_dir=project,
            config={"engine": {"reuse_session": "always"}},
            logs_dir=project / ".agentic" / "logs",
        )
    # The pair key was bumped under the session key, not the stage name.
    state = pipeline_engine.read_state(project)
    assert "TODO-0001:agent-qa-review" in state["stage_attempts"]
    assert "TODO-0001:checklist-check" not in state.get("stage_attempts", {})
