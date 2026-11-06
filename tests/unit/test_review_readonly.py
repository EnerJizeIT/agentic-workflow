"""A67: a review stage writes reports, not code.

``write_check: readonly`` fences the stage to the engine's two report
directories — ``.agentic/outbox/`` and ``.agentic/context/``. A write
anywhere else takes the existing BLOCKED path (STAGE-DIFF report, no
commit); a stage without the key keeps the old, unfenced behavior. A review
role also loses the queue tools (dispatch / todo-update / todo-remove).
"""
from __future__ import annotations

import json
from pathlib import Path

from awf import pipeline_engine
from awf._env import awf_subprocess_env
from awf.pipeline import Stage, validate_pipeline_stages
from tests.spec_support import init_project_repo

TODO = "TODO-0001"
QUEUE_TOOLS = (
    "agent-workflow-ui_awf_dispatch_todo",
    "agent-workflow-ui_awf_todo_update",
    "agent-workflow-ui_awf_todo_remove",
)


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


# ─── the allowlist ─────────────────────────────────────────────────────


def test_readonly_allowlist_is_exactly_the_report_directories():
    stage = Stage(name="agent-qa-review", role="agent-qa-review",
                  write_check="readonly")
    assert pipeline_engine._stage_allowlist(stage) == (
        ".agentic/outbox/",
        ".agentic/context/",
    )


def test_readonly_wins_over_a_declared_output():
    stage = Stage(
        name="agent-qa-review",
        role="agent-qa-review",
        output="docs/review.md",
        write_check="readonly",
    )
    assert pipeline_engine._stage_allowlist(stage) == (
        ".agentic/outbox/",
        ".agentic/context/",
    )


def test_stage_without_write_check_keeps_the_old_allowlist():
    plain = Stage(name="worker", role="worker", kind="execute")
    assert pipeline_engine._stage_allowlist(plain) == ()
    fenced = Stage(
        name="worker", role="worker", kind="execute", output="docs/out.md"
    )
    assert pipeline_engine._stage_allowlist(fenced) == ("docs/out.md",)


def test_loader_accepts_readonly():
    stages = validate_pipeline_stages(
        [{"role": "agent-qa-review", "write_check": "readonly"}], "t.yaml"
    )
    assert stages[0]["write_check"] == "readonly"


def test_generated_pipeline_marks_reviewer_stages_readonly():
    """A3: the form generator is the third surface — templates alone are not."""
    from awf.api.setup import build_pipeline_stages

    stages = build_pipeline_stages([
        {"agent": "agent-implementer"},
        {"agent": "agent-qa-review"},
        {"agent": "agent-project-auditor"},
        {"agent": "agent-requirements-reviewer"},
    ])
    by_name = {stage["name"]: stage for stage in stages}
    assert by_name["agent-qa-review"]["write_check"] == "readonly"
    assert by_name["agent-project-auditor"]["write_check"] == "readonly"
    assert by_name["checklist-check"]["write_check"] == "readonly"
    assert "write_check" not in by_name["agent-implementer"]
    assert "write_check" not in by_name["plan"]
    # The requirements-reviewer writes its own review document into the
    # spec package — a readonly fence would stop that write.
    assert "write_check" not in by_name["agent-requirements-reviewer"]
    # One composition: the generator emits the same key the shipped
    # templates carry. On a supervisor stage it is declarative (no
    # manifest), but two surfaces must not disagree on the keys.
    assert by_name["verify"]["write_check"] == "readonly"


# ─── the engine verdict ────────────────────────────────────────────────


def _patch_engine(monkeypatch, worker, calls: dict):
    def fake_escalate(project_dir, logs_dir, s_name, todo, *a, **kw):
        calls["escalate"] = s_name
        return (1, todo, 0)

    def fake_next(project_dir, logs_dir, s_name, todo, action, auto,
                  retry_counts, stage_idx):
        calls["next"] = s_name
        return (stage_idx + 1, 0)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_handle_escalate", fake_escalate)
    monkeypatch.setattr(pipeline_engine, "_handle_next", fake_next)


def test_code_write_stops_a_readonly_stage(tmp_path, monkeypatch):
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(
        name="agent-qa-review",
        role="agent-qa-review",
        kind="execute",
        write_check="readonly",
    )

    def worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (project_dir / "awf").mkdir(exist_ok=True)
        (project_dir / "awf" / "x.py").write_text("x = 1\n")
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    calls: dict = {}
    _patch_engine(monkeypatch, worker, calls)

    pipeline_engine.execute_agent_stage(
        stage=stage,
        current_todo=TODO,
        project_dir=repo,
        config={},
        logs_dir=repo / ".agentic" / "logs",
        stages=[stage],
        stage_idx=0,
        retry_counts=[0],
        auto=False,
        agent_hard_timeout=None,
    )

    assert calls.get("escalate") == "agent-qa-review", "the DONE must not override the fence"
    report = (
        repo / ".agentic" / "context" / f"STAGE-DIFF-{TODO}-agent-qa-review.md"
    )
    assert report.is_file()
    assert "awf/x.py" in report.read_text(encoding="utf-8")
    assert (repo / "awf" / "x.py").is_file(), "the violating file stays in the tree"


def test_report_write_is_not_a_violation(tmp_path):
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    stage = Stage(
        name="agent-qa-review", role="agent-qa-review",
        kind="execute", write_check="readonly",
    )
    before = pipeline_engine._stage_manifest_before(repo, stage)
    (outbox / f"DONE-{TODO}.md").write_text("# report\n", encoding="utf-8")
    (repo / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
    (repo / ".agentic" / "context" / "note.txt").write_text("x\n")

    assert pipeline_engine._stage_write_violation(
        repo, TODO, "agent-qa-review", before, stage
    ) == []


def test_a_stage_without_write_check_is_unchanged(tmp_path, monkeypatch):
    """Regression: the old pipeline snapshot never heard of ``readonly``."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute")

    def worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (project_dir / "awf").mkdir(exist_ok=True)
        (project_dir / "awf" / "x.py").write_text("x = 1\n")
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    calls: dict = {}
    _patch_engine(monkeypatch, worker, calls)

    pipeline_engine.execute_agent_stage(
        stage=stage,
        current_todo=TODO,
        project_dir=repo,
        config={},
        logs_dir=repo / ".agentic" / "logs",
        stages=[stage],
        stage_idx=0,
        retry_counts=[0],
        auto=False,
        agent_hard_timeout=None,
    )

    assert calls.get("next") == "worker"
    assert "escalate" not in calls


# ─── the role's tools ──────────────────────────────────────────────────


def _env_rules(tmp_path, monkeypatch, role: str) -> dict[str, str]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / ".agentic").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agentic" / "config.yaml").write_text(
        "project:\n  name: test\n", encoding="utf-8"
    )
    payload = json.loads(
        awf_subprocess_env(role=role, project_dir=tmp_path)["OPENCODE_CONFIG_CONTENT"]
    )
    return {
        rule["action"]: rule["effect"]
        for rule in payload["permissions"]
        if rule["resource"] == "*"
    }


def test_review_role_cannot_edit_the_queue(tmp_path, monkeypatch):
    rules = _env_rules(tmp_path, monkeypatch, "agent-qa-review")
    for tool in QUEUE_TOOLS:
        assert rules.get(tool) == "deny", tool


def test_supervisor_keeps_the_queue_tools(tmp_path, monkeypatch):
    rules = _env_rules(tmp_path, monkeypatch, "supervisor")
    for tool in QUEUE_TOOLS:
        assert rules.get(tool) != "deny", tool
