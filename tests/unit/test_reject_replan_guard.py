"""A34: the replan it does spawn is bounded, survivable and never revives.

Split out of ``test_reject_rollback.py`` when the contract file crossed the
400-line ratchet: the refusal itself (A32) is pinned there, this file pins
the replan path — bounded context, a crashed subprocess that does not stop
the run, the failure surfaced in ``awf status``, a retired id refused, and
the report's owner as the rewind target (S14 × A32).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import paths, pipeline_engine
from awf.pipeline import Stage


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for sub in ("inbox", "outbox", "context", "state", "logs", "handoff"):
        (project / ".agentic" / sub).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text("project:\n  name: A34\n")
    (project / ".agentic" / "inbox" / "TODO-0001.md").write_text(
        "---\nverify: [\"true\"]\n---\n# TODO — x\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / "TODO-0001.ready").write_text("", encoding="utf-8")
    (project / ".agentic" / "outbox" / "PROGRESS-TODO-0001.md").write_text(
        "## Task 1 · [x]\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=project, check=True)
    (project / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=project, check=True)
    return project


def _stages(on_rejected: str = "escalate") -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="implement", role="agent-implementer", kind="execute"),
        Stage(
            name="review",
            role="agent-architect-review",
            kind="execute",
            on_rejected=on_rejected,
        ),
        Stage(name="verify", role="supervisor", kind="verify", on_rejected="replan"),
    ]


def _review(project: Path) -> None:
    (project / ".agentic" / "outbox" / "REVIEW-TODO-0001.md").write_text(
        "# REVIEW\n\n- CTX1: the empty name breaks the log\n", encoding="utf-8"
    )


def _refined_unit(project: Path, todo_id: str = "TODO-0002") -> None:
    (paths.inbox(project) / f"{todo_id}.md").write_text(
        "---\nreplan_for: TODO-0001\n---\n# TODO — refined\n", encoding="utf-8"
    )
    (paths.inbox(project) / f"{todo_id}.ready").write_text("", encoding="utf-8")


def test_a_crashed_replan_continues_the_run(tmp_path, monkeypatch):
    """Acceptance: a failed replan subprocess does not stop the run.

    The reason lands in the log, in the pipeline state (``awf status``
    reads it) and in PROGRESS.
    """
    project = _project(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError(
            "Supervisor replan subprocess exited with code 1. "
            "Provider request failed with HTTP 400"
        )

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", _boom)

    idx, todo, rc = pipeline_engine._handle_rollback(
        project,
        paths.logs_dir(project),
        _stages(),
        "TODO-0001",
        auto=True,
        target="implement",
        source="review",
    )

    assert (idx, todo, rc) == (1, "TODO-0001", 0)
    state = (project / ".agentic" / "state" / "current.yaml").read_text(
        encoding="utf-8"
    )
    assert "replan_error" in state and "HTTP 400" in state
    progress = (paths.outbox(project) / "PROGRESS-TODO-0001.md").read_text(
        encoding="utf-8"
    )
    assert "replan after rollback failed" in progress
    log = (paths.logs_dir(project) / "orchestrator.log").read_text(encoding="utf-8")
    assert "A34:" in log


def test_status_shows_the_failed_replan(tmp_path):
    """Acceptance: ``awf status`` carries the reason of a failed replan."""
    from awf import api

    project = _project(tmp_path)
    pipeline_engine._write_state(
        project,
        replan_error="Supervisor replan subprocess exited with code 1: HTTP 400",
        logs_dir=paths.logs_dir(project),
    )

    status = api.get_status(project)

    assert "HTTP 400" in (status.replan_error or "")


def test_a_successful_replan_clears_the_failure_record(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _refined_unit(project)
    pipeline_engine._write_state(
        project, replan_error="old failure", logs_dir=paths.logs_dir(project)
    )
    monkeypatch.setattr(
        pipeline_engine, "_run_supervisor_stage", lambda *a, **k: "TODO-0002"
    )

    idx, todo, rc = pipeline_engine._handle_rollback(
        project,
        paths.logs_dir(project),
        _stages("replan"),
        "TODO-0001",
        auto=True,
        target="implement",
        source="review",
    )

    assert (idx, todo, rc) == (1, "TODO-0002", 0)
    state = (project / ".agentic" / "state" / "current.yaml").read_text(
        encoding="utf-8"
    )
    assert "old failure" not in state


def test_a_new_unit_appears_in_the_log_as_one_line(tmp_path, monkeypatch):
    project = _project(tmp_path)
    _refined_unit(project)
    monkeypatch.setattr(
        pipeline_engine, "_run_supervisor_stage", lambda *a, **k: "TODO-0002"
    )

    pipeline_engine._handle_escalate(
        project,
        paths.logs_dir(project),
        "review",
        "TODO-0001",
        True,
        _stages("replan")[2],
        [0] * 4,
        2,
    )

    log = (paths.logs_dir(project) / "orchestrator.log").read_text(encoding="utf-8")
    assert "replan: новый юнит TODO-0002 по причине отказа стадии review" in log


def test_a_retired_unit_is_never_reused(tmp_path):
    """Acceptance: a retired id does not come back as a clone."""
    project = _project(tmp_path)
    retired = paths.done_dir(project) / "TODO-0002"
    retired.mkdir(parents=True)
    (retired / "RETIRED-20261101T000000Z.md").write_text(
        "# RETIRED\n\ncloned the same work\n", encoding="utf-8"
    )

    assert pipeline_engine._replan_answer("TODO-0002", "TODO-0001", project) == ""
    assert pipeline_engine._replan_answer("TODO-0003", "TODO-0001", project) == "TODO-0003"


@pytest.mark.parametrize("pending_id", ["TODO-0002"])
def test_a_retired_answer_stops_instead_of_cloning(tmp_path, pending_id, monkeypatch):
    project = _project(tmp_path)
    retired = paths.done_dir(project) / pending_id
    retired.mkdir(parents=True)
    (retired / "RETIRED-20261101T000000Z.md").write_text("# RETIRED\n", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_engine, "_run_supervisor_stage", lambda *a, **k: pending_id
    )

    idx, todo, rc = pipeline_engine._handle_rollback(
        project,
        paths.logs_dir(project),
        _stages(),
        "TODO-0001",
        auto=True,
        target="implement",
        source="review",
    )

    assert (idx, todo, rc) == (-1, "TODO-0001", 1)


def test_the_fix_round_appends_while_the_engine_owns_the_unit(tmp_path, monkeypatch):
    """B1 (architect-review): a live run can append its own review section.

    Every outside writer must keep the "the engine owns the file" refusal,
    but the engine's fix round IS that owner — otherwise the 01.11 loop
    reproduces: the refusal stops the run before the rewind.
    """
    from awf.api import hygiene

    project = _project(tmp_path)
    _review(project)
    monkeypatch.setattr(
        hygiene, "check_pipeline_running", lambda _p: (True, 4242, "x")
    )
    monkeypatch.setattr(hygiene, "read_state", lambda _p: {"todo_id": "TODO-0001"})
    assert hygiene.unit_readiness(project, "TODO-0001").live is True

    idx, todo, rc = pipeline_engine._handle_rejected(
        project,
        paths.logs_dir(project),
        _stages(),
        "TODO-0001",
        _stages()[2],
        2,
    )

    assert (idx, todo, rc) == (1, "TODO-0001", 0)
    assert "## REVIEW-фикс" in (
        paths.inbox(project) / "TODO-0001.md"
    ).read_text(encoding="utf-8")
    # The public writer still refuses the same live unit.
    with pytest.raises(Exception, match="live pipeline"):
        hygiene.review_append(project, "TODO-0001", "outside fix")


def test_the_fix_round_reads_the_rejected_spelling_too(tmp_path):
    """B4 (architect-review): ``REVIEW-REJECTED-<id>.md`` is a real report.

    Older runs wrote the report next to the signal under the second name;
    a reader that knew only ``REVIEW-<id>.md`` lost the findings and
    stopped the run with "no findings to hand back".
    """
    project = _project(tmp_path)
    (paths.outbox(project) / "REVIEW-REJECTED-TODO-0001.md").write_text(
        "# REVIEW\n\n- RR1: the failure path is missing\n", encoding="utf-8"
    )

    idx, todo, rc = pipeline_engine._handle_rejected(
        project,
        paths.logs_dir(project),
        _stages(),
        "TODO-0001",
        _stages()[2],
        2,
    )

    assert (idx, todo, rc) == (1, "TODO-0001", 0)
    assert "RR1" in (paths.inbox(project) / "TODO-0001.md").read_text(
        encoding="utf-8"
    )


# ─── the report's owner is the rewind target (S14 × A32) ────────────────


def _readiness_project(project: Path) -> tuple[str, list[Stage]]:
    """A readiness stage whose BLOCKED report names the architect."""
    output = "docs/spec/x.readiness.md"
    (project / "docs" / "spec").mkdir(parents=True)
    (project / output).write_text(
        "# Готовность\n\nСтатус: BLOCKED\n\n## Открытые вопросы\n\n"
        "| ID | Вопрос | Владелец | Влияние |\n|---|---|---|---|\n"
        "| Q1 | Пробел | архитектор | блокирует R1 |\n",
        encoding="utf-8",
    )
    stages = [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="implement", role="agent-implementer", kind="execute"),
        Stage(name="design", role="agent-architector", kind="execute"),
        # An execute stage between the owner and the reviewer: the nearest
        # earlier author would be this one, so only the report's owner
        # (design) proves the widened branch is in force.
        Stage(name="tests", role="agent-qa-review", kind="execute"),
        Stage(
            name="readiness",
            role="agent-requirements-reviewer",
            kind="execute",
            output=output,
            on_rejected="escalate",
        ),
    ]
    return output, stages


def _run_readiness(project: Path, stages: list[Stage], worker):
    from awf import pipeline_engine as engine

    return engine.execute_agent_stage(
        stage=stages[4],
        current_todo="TODO-0001",
        project_dir=project,
        config={},
        logs_dir=paths.logs_dir(project),
        stages=stages,
        stage_idx=4,
        retry_counts=[0] * len(stages),
        auto=True,
        agent_hard_timeout=None,
    )


def _wire_readiness(monkeypatch, worker, fail_message: str) -> None:
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", worker)
    monkeypatch.setattr(
        pipeline_engine, "_run_supervisor_stage",
        lambda *_a, **_k: pytest.fail(fail_message),
    )
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *_a, **_k: None)
    monkeypatch.setattr(
        pipeline_engine, "_resolve_prev_handoffs", lambda *_a, **_k: []
    )
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *_a, **_k: None)


def test_a_readiness_verdict_with_escalate_rewinds_to_the_report_owner(
    tmp_path, monkeypatch
):
    """A32 × S14: a BLOCKED report rewinds to its owner, not nearest author.

    Before the widening, ``on_rejected: escalate`` on a declared verdict
    skipped the report's owner and landed on whatever execute stage sat
    just before the reviewer.
    """
    project = _project(tmp_path)
    _output, stages = _readiness_project(project)
    (paths.agentic_dir(project) / "handoff" / "readiness-TODO-0001.md").write_text(
        "# handoff\n\n- ARCH1: the design misses the failure path\n",
        encoding="utf-8",
    )

    def worker(_stage, todo_id, project_dir, _config, _logs_dir, **_kw):
        (paths.outbox(project_dir) / f"DONE-{todo_id}.ready").write_text("")

    _wire_readiness(
        monkeypatch, worker, "a verdict must not spawn the replan"
    )

    todo, idx, rc = _run_readiness(project, stages, worker)

    assert rc == 0
    assert todo == "TODO-0001"
    assert stages[idx].name == "design"
    assert "ARCH1" in (paths.inbox(project) / "TODO-0001.md").read_text(
        encoding="utf-8"
    )


def test_a_review_rejected_signal_also_lands_on_the_report_owner(
    tmp_path, monkeypatch
):
    """B2 (architect-review): the owner comes from the report, not the signal.

    A worker that emits ``REVIEW-REJECTED`` instead of a DONE + verdict
    document still leaves the same readiness report. Reading the owner only
    on the ``auto_rollback`` path sent the package to the nearest execute
    stage (``tests``) instead of the author the report names (``design``).
    """
    project = _project(tmp_path)
    _output, stages = _readiness_project(project)

    def worker(_stage, todo_id, project_dir, _config, _logs_dir, **_kw):
        for name in (
            "REVIEW-REJECTED-{todo}.md",
            "REVIEW-REJECTED-{todo}.ready",
        ):
            (paths.outbox(project_dir) / name.format(todo=todo_id)).write_text(
                "# review\n\n- ARCH1: the finding\n", encoding="utf-8"
            )

    _wire_readiness(
        monkeypatch, worker, "a rejection must not spawn the replan"
    )

    todo, idx, rc = _run_readiness(project, stages, worker)

    assert rc == 0
    assert todo == "TODO-0001"
    assert stages[idx].name == "design"
