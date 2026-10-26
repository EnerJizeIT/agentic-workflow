"""SPEC IMP5 / AUD4 — fail-closed write manifest (QA TODO-0256).

The pre- and post-stage snapshots both owe the same failure answer: a
manifest that cannot be taken turns the stage into ``BLOCKED``. The stage
must not run unfenced, the cause must reach the supervisor before the
replan, and the commit gate must refuse.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from awf import pipeline_engine
from awf.pipeline import Stage
from tests.spec_support import init_project_repo

TODO = "TODO-0001"


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def _patch_engine(monkeypatch, repo: Path, worker, state_calls: list[dict]):
    escalations: list[str] = []

    def fake_escalate(project_dir, logs_dir, s_name, todo, *a, **kw):
        escalations.append(s_name)
        return (1, todo, 0)

    def fake_state(project_dir, **kwargs):
        state_calls.append(kwargs)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_write_state", fake_state)
    monkeypatch.setattr(pipeline_engine, "_handle_escalate", fake_escalate)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    return escalations


def _run(repo: Path, stage: Stage) -> tuple[str, int, int]:
    return pipeline_engine.execute_agent_stage(
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


# --- AUD4 follow-ups (QA TODO-0256) ------------------------------------


def test_missing_git_binary_blocks_instead_of_crashing(tmp_path, monkeypatch):
    """AUD4 names "no git" as a manifest failure — BLOCKED, not a traceback.

    A missing ``git`` binary raises ``FileNotFoundError`` from
    ``subprocess.run`` (only a timeout and a non-zero rc were normalized),
    so the pre-stage snapshot escaped ``execute_agent_stage`` as a raw
    exception instead of the fail-closed BLOCKED path.
    """
    from awf import commit_plan, stage_manifest

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    worker_runs: list[str] = []

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        worker_runs.append(todo_id)
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    class NoGit:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(*args, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", "git")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, repo, fake_worker, state_calls)
    monkeypatch.setattr(stage_manifest, "subprocess", NoGit)

    _run(repo, stage)

    assert worker_runs == [], "without git the fence cannot be taken"
    assert escalations == ["worker"]
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{TODO}.md"
    assert note.is_file()
    assert "No such file or directory" in note.read_text(encoding="utf-8")
    assert any(
        call.get("last_signal", "").startswith("BLOCKED-") for call in state_calls
    )
    assert commit_plan.build_commit_plan(repo, TODO).refusal


def test_after_snapshot_failure_blocks_the_stage(tmp_path, monkeypatch):
    """AUD4: the AFTER snapshot failing is the same BLOCKED, not a skipped check."""
    from awf import commit_plan, stage_manifest

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    calls = {"n": 0}
    real_snapshot = stage_manifest.snapshot

    def flaky_snapshot(project_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_snapshot(project_dir)
        raise RuntimeError("git ls-files timed out")

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, repo, fake_worker, state_calls)
    monkeypatch.setattr(stage_manifest, "snapshot", flaky_snapshot)

    _run(repo, stage)

    assert calls["n"] == 2, "the stage ran; only the AFTER snapshot failed"
    assert escalations == ["worker"]
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{TODO}.md"
    assert note.is_file()
    text = note.read_text(encoding="utf-8")
    assert "git ls-files timed out" in text
    assert "прошла без забора" in text, "the stage DID run — the note must say so"
    assert any(
        call.get("last_signal", "").startswith("BLOCKED-") for call in state_calls
    )
    record = repo / ".agentic" / "context" / f"WRITE-VIOLATION-{TODO}.json"
    assert json.loads(record.read_text(encoding="utf-8"))["error"]
    assert commit_plan.build_commit_plan(repo, TODO).refusal


def test_blocked_cause_reaches_the_supervisor_before_the_replan(
    tmp_path, monkeypatch
):
    """AUD1: note + STAGE-DIFF + last_signal exist when the replan starts."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    at_replan: dict[str, object] = {}

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret = 1\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    _patch_engine(monkeypatch, repo, fake_worker, state_calls)

    def observing_escalate(project_dir, logs_dir, s_name, todo, *a, **kw):
        at_replan["note"] = (repo / ".agentic" / "outbox" / f"BLOCKED-{todo}.md").is_file()
        at_replan["diff"] = (
            repo / ".agentic" / "context" / f"STAGE-DIFF-{todo}-{s_name}.md"
        ).is_file()
        at_replan["signal"] = state_calls[-1].get("last_signal") if state_calls else None
        return (1, todo, 0)

    monkeypatch.setattr(pipeline_engine, "_handle_escalate", observing_escalate)
    _run(repo, stage)

    assert at_replan["note"], "the replan reads the BLOCKED note — it must exist first"
    assert at_replan["diff"], "the STAGE-DIFF report must exist before the replan"
    assert str(at_replan["signal"] or "").startswith("BLOCKED-")
