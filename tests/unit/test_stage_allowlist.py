"""SPEC IMP5 — K5 stage write allowlist (T10/T11, AC7/AC15/AC16/AC18).

A stage that declares ``output``/``write_allow`` may write only inside the
allowlist. A path outside it turns the stage into ``BLOCKED``: the file
stays, the signal does not override it, and no commit happens. Gitignored
and engine-runtime files are not the worker's write.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from awf import pipeline_engine
from awf.api._errors import AwfApiError
from awf.pipeline import Stage, validate_pipeline_stages
from awf.pipeline_engine import _stage_write_violation
from tests.spec_support import git, init_project_repo

TODO = "TODO-0001"


# --- loader (AC18) ------------------------------------------------------


def _stage(**extra) -> dict:
    base = {"role": "worker", "output": "docs/spec/pkg/out.md"}
    base.update(extra)
    return base


def test_loader_accepts_paths_inside_output_directory():
    stages = validate_pipeline_stages(
        [_stage(write_allow=["docs/spec/pkg/assets"])], "t.yaml"
    )
    assert stages[0]["write_allow"] == ["docs/spec/pkg/assets"]


def test_loader_accepts_the_service_run_candidate_output():
    """The one ``.agentic`` output is the role-draft candidate (M5.2)."""
    stages = validate_pipeline_stages(
        [{"role": "worker", "output": ".agentic/roles/draft/auditor.md"}], "t.yaml"
    )
    assert stages[0]["output"] == ".agentic/roles/draft/auditor.md"


@pytest.mark.parametrize(
    ("stage", "needle"),
    [
        ({"write_allow": ["src/x.py"]}, "outside its output directory"),
        ({"write_allow": [".agentic/state/x"]}, ".agentic/"),
        ({"write_allow": ["docs/.git/x"]}, ".git/"),
        ({"write_allow": ["docs/spec/pkg/../x"]}, "project-relative"),
        ({"write_allow": ["/abs/x"]}, "project-relative"),
        ({"write_allow": []}, "non-empty list"),
        ({"write_allow": ["docs/spec/pkg/x"], "output": ""}, "'output'"),
        ({"output": ".git/x.md"}, ".git/"),
        ({"output": "docs/.git/x.md"}, ".git/"),
        ({"output": ".agentic/state/x.md"}, ".agentic/"),
    ],
)
def test_loader_refuses_invalid_write_allow(stage, needle):
    with pytest.raises(AwfApiError) as excinfo:
        validate_pipeline_stages([_stage(**stage)], "t.yaml")
    assert needle in str(excinfo.value)


# --- engine verdict (AC15/AC16) ----------------------------------------


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


def test_path_outside_allowlist_blocks_stage(tmp_path, monkeypatch):
    """prove_red R5: a write outside the allowlist is BLOCKED, file kept."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    (repo / "docs" / "spec" / "pkg").mkdir(parents=True)
    stage = Stage(
        name="worker",
        role="worker",
        kind="execute",
        output="docs/spec/pkg/out.md",
        write_allow=("docs/spec/pkg",),
    )
    before_commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "docs" / "spec" / "pkg" / "out.md").write_text("ok\n")
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret = 1\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")
        (repo / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text("x\n")

    escalations: list[str] = []

    def fake_escalate(project_dir, logs_dir, s_name, todo, *a, **kw):
        escalations.append(s_name)
        return (1, todo, 0)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", fake_worker)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_handle_escalate", fake_escalate)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    todo, _idx, rc = pipeline_engine.execute_agent_stage(
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

    assert escalations == ["worker"], "the DONE signal must not override the diff"
    assert rc == 0
    diff_report = (
        repo / ".agentic" / "context" / "STAGE-DIFF-TODO-0001-worker.md"
    )
    assert diff_report.is_file()
    assert "src/evil.py" in diff_report.read_text(encoding="utf-8")
    violations_part = diff_report.read_text(encoding="utf-8").split("Allowlist:")[0]
    assert "docs/spec/pkg/out.md" not in violations_part
    assert (repo / "src" / "evil.py").is_file(), "the file must stay in the tree"
    after_commits = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after_commits == before_commits, "a violating stage must not commit"


def test_ignored_and_runtime_files_are_not_violations(tmp_path):
    repo = init_project_repo(tmp_path)
    (repo / ".gitignore").write_text("*.log\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "ignore logs")
    _outbox(repo)
    stage = Stage(
        name="worker",
        role="worker",
        kind="execute",
        output="docs/out.md",
        write_allow=("docs",),
    )
    from awf import stage_manifest

    before = stage_manifest.snapshot(repo)
    (repo / "debug.log").write_text("noise\n")
    (repo / ".agentic" / "outbox" / "DONE-TODO-0001.ready").write_text("")
    bad = _stage_write_violation(repo, TODO, "worker", before, stage)
    assert bad == []
    assert not (repo / ".agentic" / "context" / f"STAGE-DIFF-{TODO}-worker.md").exists()



def test_output_only_stage_gets_a_write_manifest(tmp_path):
    """K5: ``output`` alone already forms an allowlist, so it must be fenced.

    QA-0254-1: this used to be xfail — enforcement started only with an
    explicit ``write_allow``, so an output-only stage walked past the fence.
    """
    repo = init_project_repo(tmp_path)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    assert pipeline_engine._stage_manifest_before(repo, stage) is not None


# --- AUD1/AUD2 (TODO-0256): the cause reaches the supervisor, the gate ---


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


def test_violation_is_visible_to_the_supervisor(tmp_path, monkeypatch):
    """AUD1: BLOCKED note + STAGE-DIFF link + last_signal marker, not DONE."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret = 1\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, repo, fake_worker, state_calls)
    _run(repo, stage)

    assert escalations == ["worker"]
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{TODO}.md"
    assert note.is_file(), "the replan reads this note, not the DONE report"
    text = note.read_text(encoding="utf-8")
    assert "src/evil.py" in text
    assert f"STAGE-DIFF-{TODO}-worker.md" in text
    assert any(
        call.get("last_signal", "").startswith("BLOCKED-") for call in state_calls
    ), "wait_event and the dashboard key off last_signal — it must not stay DONE"


def test_open_violation_blocks_the_commit_until_rolled_back(tmp_path, monkeypatch):
    """AUD2: the record outlives the attempt and holds the commit gate."""
    from awf import commit_plan

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret = 1\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    state_calls: list[dict] = []
    _patch_engine(monkeypatch, repo, fake_worker, state_calls)
    _run(repo, stage)

    record = repo / ".agentic" / "context" / f"WRITE-VIOLATION-{TODO}.json"
    assert record.is_file(), "the gate reads this record, not the attempt"
    plan = commit_plan.build_commit_plan(repo, TODO)
    assert plan.refusal, "an open violation must refuse the commit"
    assert "src/evil.py" in plan.refusal

    # A second build on the same (already violated) tree is still refused —
    # the attempt boundary is gone, the record is not.
    (repo / "src" / "evil.py").write_text("edited again\n")
    assert commit_plan.build_commit_plan(repo, TODO).refusal == "", (
        "a rewritten path is no longer the rejected write"
    )


def test_a_second_violation_does_not_erase_the_first(tmp_path, monkeypatch):
    """AUD2: an ACK re-run that writes another bad path keeps the first open."""
    from awf import commit_plan

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")

    def first_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src").mkdir(exist_ok=True)
        (repo / "src" / "evil.py").write_text("secret = 1\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    _patch_engine(monkeypatch, repo, first_worker, [])
    _run(repo, stage)

    def second_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (repo / "src" / "evil2.py").write_text("secret = 2\n")
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", second_worker)
    _run(repo, stage)

    record = repo / ".agentic" / "context" / f"WRITE-VIOLATION-{TODO}.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    assert {"src/evil.py", "src/evil2.py"} <= set(data["paths"]), (
        "the second violation must merge, not replace the record"
    )

    (repo / "src" / "evil2.py").write_text("fixed\n")
    plan = commit_plan.build_commit_plan(repo, TODO)
    assert plan.refusal
    assert "src/evil.py" in plan.refusal, (
        "the first rejected path still withholds the commit"
    )


def test_manifest_failure_blocks_instead_of_skipping_the_check(tmp_path, monkeypatch):
    """AUD4: no manifest → BLOCKED; the worker is not run unfenced."""
    from awf import commit_plan, stage_manifest

    repo = init_project_repo(tmp_path)
    _outbox(repo)
    stage = Stage(name="worker", role="worker", kind="execute", output="docs/out.md")
    worker_runs: list[str] = []

    def fake_worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        worker_runs.append(todo_id)
        (repo / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")

    def broken_snapshot(project_dir):
        raise RuntimeError("git ls-files timed out")

    state_calls: list[dict] = []
    escalations = _patch_engine(monkeypatch, repo, fake_worker, state_calls)
    monkeypatch.setattr(stage_manifest, "snapshot", broken_snapshot)

    _run(repo, stage)

    assert worker_runs == [], "a stage without a manifest must not run unfenced"
    assert escalations == ["worker"]
    note = repo / ".agentic" / "outbox" / f"BLOCKED-{TODO}.md"
    assert note.is_file()
    assert "манифеста" in note.read_text(encoding="utf-8")
    assert any(
        call.get("last_signal", "").startswith("BLOCKED-") for call in state_calls
    )
    assert commit_plan.build_commit_plan(repo, TODO).refusal


# --- the named opt-out (TODO-0256) -------------------------------------


def test_write_check_off_is_the_only_opt_out(tmp_path):
    from awf.pipeline import validate_pipeline_stages

    stage = Stage(
        name="worker",
        role="worker",
        kind="execute",
        output="docs/out.md",
        write_check="off",
    )
    assert pipeline_engine._stage_allowlist(stage) == ()
    assert pipeline_engine._stage_manifest_before(tmp_path, stage) is None

    stages = validate_pipeline_stages(
        [{"role": "worker", "output": "docs/out.md", "write_check": "off"}], "t.yaml"
    )
    assert stages[0]["write_check"] == "off"

    for bogus in (False, "no", 0):
        with pytest.raises(AwfApiError) as excinfo:
            validate_pipeline_stages(
                [{"role": "worker", "output": "docs/out.md", "write_check": bogus}],
                "t.yaml",
            )
        assert "write_check" in str(excinfo.value)
