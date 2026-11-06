"""A67: a reviewer's structural findings are the stage's verdict.

The contract (schema, ownership, delivery to the fix round) is pinned in
``test_findings_verdict_contract.py``. Here the decision itself: the
threshold, the "report by presence" rule, and the engine applying it — for
a worker review stage and for the supervisor's verify.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf import pipeline_engine
from awf import reject_files as awf_reject_files
from awf.config import findings_threshold
from awf.pipeline import Stage
from tests.spec_support import init_project_repo

TODO = "TODO-0001"


def _outbox(repo: Path) -> Path:
    outbox = repo / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


# ─── the threshold ─────────────────────────────────────────────────────


def test_threshold_defaults_to_major():
    assert findings_threshold({}) == "major"
    assert findings_threshold(
        {"review": {"findings_threshold": "blocker"}}
    ) == "blocker"


def test_unknown_threshold_falls_back_to_major():
    """A typo must not switch the lenient mode on by accident."""
    assert findings_threshold(
        {"review": {"findings_threshold": "high"}}
    ) == "major"


@pytest.mark.parametrize(
    ("findings", "threshold", "review_present", "rejected"),
    [
        ([{"severity": "major"}], "major", False, True),
        ([{"severity": "major"}], "blocker", False, False),
        ([{"severity": "minor"}], "major", False, False),
        ([{"severity": "blocker"}], "blocker", False, True),
        ([{"severity": "критично"}], "blocker", False, True),
        ([], "major", False, False),
        ([], "major", True, True),
    ],
)
def test_review_findings_rejected(findings, threshold, review_present, rejected):
    assert (
        pipeline_engine.review_findings_rejected(
            findings,
            threshold=threshold,
            review_present=review_present,
        )
        is rejected
    )


def test_review_file_alone_rejects(tmp_path):
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    (outbox / f"REVIEW-{TODO}.md").write_text("# rejected\n", encoding="utf-8")
    assert pipeline_engine._review_stage_rejects(repo, TODO, {})


def test_a_stale_review_report_does_not_reject_a_passing_stage(tmp_path):
    """Only this stage's report is its verdict."""
    repo = init_project_repo(tmp_path)
    outbox = _outbox(repo)
    report = outbox / f"REVIEW-{TODO}.md"
    report.write_text("# rejected last time\n", encoding="utf-8")

    assert not pipeline_engine._review_stage_rejects(
        repo, TODO, {}, since=report.stat().st_mtime + 60
    )


# ─── the engine applies the verdict ────────────────────────────────────


def _patch_engine(monkeypatch, worker, calls: dict):
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_write_state", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    def fake_fix_round(project_dir, logs_dir, stages, todo, target, source="",
                       **kwargs):
        calls["fix_round"] = target
        return (0, todo, 0)

    def fake_next(project_dir, logs_dir, s_name, todo, action, auto,
                  retry_counts, stage_idx):
        calls["next"] = s_name
        return (stage_idx + 1, 0)

    monkeypatch.setattr(pipeline_engine, "_handle_fix_round", fake_fix_round)
    monkeypatch.setattr(pipeline_engine, "_handle_next", fake_next)


def _review_stage() -> Stage:
    return Stage(
        name="agent-qa-review",
        role="agent-qa-review",
        kind="execute",
        on_rejected="fix_round:author",
    )


def test_major_finding_rejects_through_the_stage_policy(tmp_path, monkeypatch):
    """The verdict only picks ``rejected`` — the stage's ``on_rejected`` runs."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    author = Stage(name="author", role="agent-implementer", kind="execute")
    review = _review_stage()

    def worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.json").write_text(
            json.dumps({"findings": [{"severity": "major"}]}), encoding="utf-8"
        )

    calls: dict = {}
    _patch_engine(monkeypatch, worker, calls)

    _todo, _idx, rc = pipeline_engine.execute_agent_stage(
        stage=review,
        current_todo=TODO,
        project_dir=repo,
        config={"review": {"findings_threshold": "major"}},
        logs_dir=repo / ".agentic" / "logs",
        stages=[author, review],
        stage_idx=1,
        retry_counts=[0, 0],
        auto=False,
        agent_hard_timeout=None,
    )

    assert rc == 0
    assert calls.get("fix_round") == "author", "a major finding must rewind the unit"
    assert "next" not in calls


def test_minor_finding_passes(tmp_path, monkeypatch):
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    author = Stage(name="author", role="agent-implementer", kind="execute")
    review = _review_stage()

    def worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.ready").write_text("")
        (project_dir / ".agentic" / "outbox" / f"DONE-{todo_id}.json").write_text(
            json.dumps({"findings": [{"severity": "minor"}]}), encoding="utf-8"
        )

    calls: dict = {}
    _patch_engine(monkeypatch, worker, calls)

    pipeline_engine.execute_agent_stage(
        stage=review,
        current_todo=TODO,
        project_dir=repo,
        config={"review": {"findings_threshold": "major"}},
        logs_dir=repo / ".agentic" / "logs",
        stages=[author, review],
        stage_idx=1,
        retry_counts=[0, 0],
        auto=False,
        agent_hard_timeout=None,
    )

    assert calls.get("next") == "agent-qa-review"
    assert "fix_round" not in calls


def test_review_findings_from_probes_reject_the_stage(tmp_path, monkeypatch):
    """A27 sends a reader stage's facts to PROBES — the verdict must see them."""
    repo = init_project_repo(tmp_path)
    _outbox(repo)
    author = Stage(name="author", role="agent-implementer", kind="execute")
    review = _review_stage()

    def worker(stage, todo_id, project_dir, config, logs_dir, **kwargs):
        outbox = project_dir / ".agentic" / "outbox"
        (outbox / f"DONE-{todo_id}.ready").write_text("")
        (outbox / f"DONE-{todo_id}.md").write_text("# review\n", encoding="utf-8")
        (outbox / f"PROBES-{todo_id}.json").write_text(
            json.dumps({"findings": [{"severity": "major"}]}), encoding="utf-8"
        )

    calls: dict = {}
    _patch_engine(monkeypatch, worker, calls)

    pipeline_engine.execute_agent_stage(
        stage=review,
        current_todo=TODO,
        project_dir=repo,
        config={"review": {"findings_threshold": "major"}},
        logs_dir=repo / ".agentic" / "logs",
        stages=[author, review],
        stage_idx=1,
        retry_counts=[0, 0],
        auto=False,
        agent_hard_timeout=None,
    )

    assert calls.get("fix_round") == "author"
    assert "next" not in calls


# ─── the verify stage (supervisor family) uses the same verdict ─────────


def _verify_stage() -> Stage:
    return Stage(
        name="verify",
        role="supervisor",
        kind="verify",
        on_approved="commit_and_next",
        on_rejected="replan",
    )


def _patch_verify(monkeypatch, calls: dict):
    """Patch the verify cycle: supervisor, state, commit, replan.

    The fake supervisor writes the findings INSIDE the stage call — the same
    order as a real worker, so the stage's freshness anchor sees them.
    """
    def fake_supervisor(stage, todo_id, auto, project_dir, logs_dir,
                        pipeline_name=None):
        calls.setdefault("sup_stages", []).append(stage.name)
        if stage.name == "replan":
            return ""
        outbox = project_dir / ".agentic" / "outbox"
        (outbox / f"PROBES-{todo_id}.json").write_text(
            json.dumps({"findings": [{"severity": "major"}]}), encoding="utf-8"
        )
        return f"ACK-{todo_id}"

    def fake_state(project_dir, **kwargs):
        if "last_signal" in kwargs:
            calls.setdefault("last_signals", []).append(kwargs["last_signal"])

    def fake_commit(*args, **kwargs):
        calls["commit"] = True
        return pipeline_engine._commit_plan.CommitOutcome(
            pipeline_engine._commit_plan.OUTCOME_SKIPPED, "test"
        )

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", fake_supervisor)
    monkeypatch.setattr(pipeline_engine, "_write_state", fake_state)
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: "")
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_maybe_commit", fake_commit)
    monkeypatch.setattr(awf_reject_files, "snapshot_rejected_files",
                        lambda *a, **kw: None)


def _verify_project(tmp_path: Path) -> Path:
    project = tmp_path / "verify"
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "inbox").mkdir(parents=True)
    return project


def _run_verify(project: Path):
    return pipeline_engine.execute_supervisor_stage(
        stage=_verify_stage(),
        current_todo=TODO,
        auto=False,
        project_dir=project,
        config={"review": {"findings_threshold": "major"}},
        logs_dir=project / ".agentic" / "logs",
    )


def test_verify_findings_reject_instead_of_committing(tmp_path, monkeypatch):
    """A major finding on the supervisor stage rejects it — no commit."""
    project = _verify_project(tmp_path)
    calls: dict = {}
    _patch_verify(monkeypatch, calls)

    _todo, _delta, rc = _run_verify(project)

    assert "commit" not in calls, "the findings verdict must not be committed"
    assert "replan" in calls.get("sup_stages", []), (
        "verify's on_rejected is the route — the replan must run"
    )
    assert rc == 1, "a rejected unit stops the run"


def test_verify_reject_consumes_the_approval_it_replaced(tmp_path, monkeypatch):
    """A4: a live ACK must not survive the findings rejection."""
    project = _verify_project(tmp_path)
    ack = project / ".agentic" / "inbox" / f"ACK-{TODO}.ready"
    ack.write_text("", encoding="utf-8")
    calls: dict = {}
    _patch_verify(monkeypatch, calls)

    _run_verify(project)

    assert not ack.exists(), (
        "a live ACK after the findings rejection: the next verify would take "
        "it as a fresh approval"
    )


def test_verify_reject_persists_the_rejection_marker(tmp_path, monkeypatch):
    """A4: state names the rejection, not the ACK it replaced."""
    project = _verify_project(tmp_path)
    calls: dict = {}
    _patch_verify(monkeypatch, calls)

    _run_verify(project)

    assert calls["last_signals"][-1] == f"REVIEW-{TODO}", (
        "state names the ACK though the stage was rejected"
    )
