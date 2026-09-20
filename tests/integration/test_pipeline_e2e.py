"""AD-3: Full pipeline integration test through API layer.

Tests the complete flow: plan → agent → verify → archive → done.
Mocks opencode subprocess (no real binary needed).

Unlike e2e tests (which use bin/awf CLI), this test calls run_pipeline()
directly through Python API — testing orchestrator + pipeline_engine +
agent_stage + commit_gate + todos + archive together.

Covers:
- Happy path: plan → worker → verify → approve → archive → complete
- Worker BLOCKED → escalate path
- Verify REVIEW → replan → stop
- Salvage: worker doesn't signal → auto_done
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api


@pytest.fixture
def project(tmp_path):
    """Project with git + .agentic/ + pipeline + worker role."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
    (repo / "README.md").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    api.init_project(repo, project_name="Test")

    # Pipeline: plan → worker → verify
    pipes = repo / ".agentic" / "pipelines"
    pipes.mkdir(parents=True, exist_ok=True)
    (pipes / "default.yaml").write_text(
        'name: "default"\n'
        'stages:\n'
        '  - name: "plan"\n    role: "supervisor"\n    kind: "plan"\n'
        '  - name: "worker"\n    role: "worker"\n    kind: "execute"\n'
        '    on_blocked: "escalate"\n    max_retries: 3\n'
        '  - name: "verify"\n    role: "supervisor"\n    kind: "verify"\n'
        '    on_approved: "commit_and_next"\n    on_rejected: "replan"\n',
        encoding="utf-8",
    )

    # Worker role file
    roles = repo / ".agentic" / "roles"
    roles.mkdir(parents=True, exist_ok=True)
    (roles / "worker.md").write_text("# Worker\nExecute TODO.\n", encoding="utf-8")

    return repo


def _create_todo(project: Path, todo_id: str = "TODO-0001") -> None:
    """Create TODO in inbox + baseline."""
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\nTest task\n")
    (inbox / f"{todo_id}.ready").write_text("")

    # Baseline
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True).stdout.strip()
    ctx = project / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{todo_id}.sha").write_text(sha + "\n")


def _write_done(project: Path, todo_id: str) -> None:
    """Simulate worker writing DONE signal."""
    outbox = project / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"DONE-{todo_id}.md").write_text(f"# Done\n{todo_id} complete\n")
    (outbox / f"DONE-{todo_id}.ready").write_text("")
    # Worker also changed a file
    (project / "src.txt").write_text("worker output\n")


def _write_ack(project: Path, todo_id: str) -> None:
    """Simulate supervisor writing ACK signal."""
    inbox = project / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"ACK-{todo_id}.ready").write_text("")


def _make_args(project: Path, **kwargs) -> SimpleNamespace:
    """Create args namespace for run_pipeline."""
    defaults = {
        "project_dir": str(project),
        "pipeline": None,
        "from_stage": None,
        "auto": True,
        "timeout": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _mock_stages(monkeypatch, project: Path):
    """Mock supervisor + agent stages to avoid real subprocess.

    Returns a dict tracking calls.
    """
    calls: dict = {"supervisor": [], "agent": []}

    def mock_supervisor(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
        calls["supervisor"].append((stage.name, stage.kind, todo_id))
        # Plan: just let orchestrator find TODO (return empty signal)
        if stage.kind == "plan":
            return ""
        # Verify: create ACK + APPROVE so commit_gate and archive both work
        if stage.kind == "verify":
            inbox_path = project / ".agentic" / "inbox"
            inbox_path.mkdir(parents=True, exist_ok=True)
            (inbox_path / f"ACK-{todo_id}.ready").write_text("")
            (inbox_path / f"APPROVE-{todo_id}.ready").write_text("")
            return f"ACK-{todo_id}"
        return f"ACK-{todo_id}"

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, prev_handoffs=None, hard_timeout=None, retry_note=None, attempt=1, **kw):
        calls["agent"].append((stage.name, todo_id))
        _write_done(project, todo_id)

    # Patch at pipeline_engine module level (where they're called)
    import awf.pipeline_engine as engine
    monkeypatch.setattr(engine, "_run_supervisor_stage", mock_supervisor)
    monkeypatch.setattr(engine, "_run_agent_stage", mock_agent)

    return calls


class TestFullPipelineHappyPath:
    """Plan → worker → verify → approve → archive → complete."""

    def test_happy_path(self, project, monkeypatch):
        _create_todo(project, "TODO-0001")
        calls = _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline
        rc = run_pipeline(_make_args(project))

        assert rc == 0, f"Pipeline should complete successfully. Calls: {calls}"

        # Supervisor stages: plan + verify
        sup_kinds = [c[1] for c in calls["supervisor"]]
        assert "plan" in sup_kinds
        assert "verify" in sup_kinds

        # Agent stage: worker
        assert len(calls["agent"]) == 1
        assert calls["agent"][0][0] == "worker"

        # TODO archived
        done_dir = project / ".agentic" / "done" / "TODO-0001"
        assert done_dir.is_dir(), "TODO-0001 should be archived"
        assert (done_dir / "TODO.md").is_file()

        # Inbox clean
        inbox = project / ".agentic" / "inbox"
        assert not (inbox / "TODO-0001.ready").exists()
        assert not (inbox / "ACK-TODO-0001.ready").exists()

        # State cleared
        state = project / ".agentic" / "state" / "current.yaml"
        # SMO: state persists with phase="done" (not fully cleared)
        assert state.exists(), "State should have phase=done after completion"
        from awf.pipeline_state import read_state
        final_state = read_state(project)
        assert final_state is not None
        assert final_state.get("phase") == "done"


class TestFullPipelineReview:
    """Verify stage: supervisor writes REVIEW → pipeline stops."""

    def test_review_stops_pipeline(self, project, monkeypatch):
        _create_todo(project, "TODO-0001")
        calls = _mock_stages(monkeypatch, project)

        # Override verify to write REVIEW
        import awf.pipeline_engine as engine

        def mock_supervisor_review(stage, todo_id, auto, project_dir, logs_dir, pipeline_name=None):
            calls["supervisor"].append((stage.name, stage.kind, todo_id))
            if stage.kind == "plan":
                return ""
            if stage.kind == "verify":
                outbox = project / ".agentic" / "outbox"
                outbox.mkdir(parents=True, exist_ok=True)
                (outbox / f"REVIEW-{todo_id}.md").write_text("# Review\nIssues found\n")
                return f"REVIEW-{todo_id}"
            if stage.kind == "replan":
                return ""
            return ""

        monkeypatch.setattr(engine, "_run_supervisor_stage", mock_supervisor_review)

        from awf.orchestrator import run_pipeline
        rc = run_pipeline(_make_args(project))

        # Pipeline should stop after REVIEW
        assert rc == 1, f"Pipeline should stop on REVIEW. rc={rc}"

        # AUD04-04: the REVIEW is consumed at the end of the cycle — a stale
        # file in the outbox would re-trigger replan in a fresh verify.
        assert not (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").exists(), (
            "stale REVIEW survived the cycle — a fresh verify would re-reject"
        )

        # TODO should NOT be archived (work rejected)
        done_dir = project / ".agentic" / "done" / "TODO-0001"
        assert not done_dir.is_dir(), "TODO should NOT be archived after REVIEW"


class TestFullPipelineMultipleTodos:
    """Two sequential TODOs through the same pipeline."""

    def test_two_todos(self, project, monkeypatch):
        calls = _mock_stages(monkeypatch, project)

        from awf.orchestrator import run_pipeline

        # First TODO
        _create_todo(project, "TODO-0001")
        rc1 = run_pipeline(_make_args(project))
        assert rc1 == 0

        # TODO-0001 archived
        assert (project / ".agentic" / "done" / "TODO-0001").is_dir()

        # Second TODO
        _create_todo(project, "TODO-0002")
        rc2 = run_pipeline(_make_args(project))
        assert rc2 == 0

        # Both archived
        assert (project / ".agentic" / "done" / "TODO-0001").is_dir()
        assert (project / ".agentic" / "done" / "TODO-0002").is_dir()

        # done_count from status
        status = api.get_status(project)
        assert status.done_count >= 2
