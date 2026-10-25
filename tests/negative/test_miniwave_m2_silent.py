"""M2 (BACKLOG, 20-21): тихие смерти сессий.

На 0224 архитектор-сессии трижды выходили без сигнала, причина была
невидима: stage-log не фиксировал ни rc, ни сигнал, ни длительность, а
движок молча перезапускал. Continue-push начинал контекст заново.

(a) stage-log несёт `worker-exit: rc=.. signal=.. duration=..s class=..`
    для silent / oom-like / network (сетевая классификация сохранена);
(b) второй silent-выход подряд → continue-push-заметка содержит reduce-
    строку, первый — нет;
(c) после бюджета — salvage;
(d) заметка содержит путь прогресса (worker-log + DONE-черновик).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import agent_stage, commit_plan, pipeline_engine
from awf.pipeline import Stage
from awf.pipeline_state import read_state, write_state

T = "TODO-0001"


# ─── (a): диагностика в stage-log ───────────────────────────────────────


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("outbox", "context", "logs", "inbox", "roles"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "roles" / "worker.md").write_text(
        "# worker\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.md").write_text(
        "# task\n", encoding="utf-8"
    )
    return project


def _stub_runner(monkeypatch, worker_text: str, rc: int):
    """Worker-stub: пишет worker-log и возвращает заданный rc."""

    def fake(cmd, cwd, **kw):
        holder = kw.get("log_holder")
        if holder is not None:
            log_file = Path(cwd) / ".agentic" / "logs" / f"awf-worker-{T}.out"
            log_file.write_text(worker_text, encoding="utf-8")
            holder["log_path"] = str(log_file)
            holder["log_start_offset"] = 0
        return subprocess.CompletedProcess(cmd, rc)

    monkeypatch.setattr("awf.signal_watch.run_subprocess_until_signal", fake)


def _run_stage(project: Path):
    stage = Stage(name="worker", role="worker", kind="execute")
    return agent_stage.run_agent_stage(
        stage, T, project, {}, project / ".agentic" / "logs",
        hard_timeout=None,
    )


def _stage_log(project: Path) -> str:
    return (project / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )


def test_silent_exit_is_logged_with_class_silent(tmp_path, monkeypatch):
    """(a) rc=0 без сигнала → worker-exit ... class=silent."""
    project = _project(tmp_path)
    _stub_runner(monkeypatch, "worker stopped talking\n", 0)

    _run_stage(project)

    log = _stage_log(project)
    assert "worker-exit: rc=0 signal=none" in log
    assert "class=silent" in log
    assert "duration=" in log and "s class=" in log


def test_oom_like_exit_is_logged(tmp_path, monkeypatch):
    """(a) rc=137 (SIGKILL/RSS) → class=oom-like, сигнал назван."""
    project = _project(tmp_path)
    _stub_runner(monkeypatch, "", 137)

    with pytest.raises(RuntimeError):
        _run_stage(project)

    log = _stage_log(project)
    assert "worker-exit: rc=137 signal=SIGKILL" in log
    assert "class=oom-like" in log


def test_network_exit_keeps_existing_classification(tmp_path, monkeypatch):
    """(a) сетевой маркер → class=network (не silent)."""
    project = _project(tmp_path)
    _stub_runner(monkeypatch, "Cannot connect to API\n", 0)

    _run_stage(project)

    log = _stage_log(project)
    assert "class=network" in log
    assert "class=silent" not in log


# ─── (b)(c)(d): bounded-ремедиация и прогресс в continue ────────────────


def _git_project(tmp_path: Path) -> tuple[Path, str]:
    """Git-репо с закоммиченным README — baseline для work-доказательств."""
    from conftest import _git_init

    from awf.git_utils import current_sha

    project = tmp_path / "repo"
    project.mkdir()
    _git_init(project)
    (project / ".agentic" / "outbox").mkdir(parents=True)
    (project / ".agentic" / "context").mkdir(parents=True)
    (project / ".agentic" / "logs").mkdir(parents=True)
    return project, current_sha(project)


def _mock_worker(monkeypatch, behavior, baseline_sha: str | None = None):
    """Привязывает stage-цикл к сценарному воркеру; возвращает журнал."""
    journal = {"runs": 0, "retry_notes": [], "monotonic": iter(range(10_000))}

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        journal["runs"] += 1
        journal["retry_notes"].append(kw.get("retry_note"))
        behavior(journal["runs"], kw)

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **kw: [])
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **kw: "")
    monkeypatch.setattr(
        pipeline_engine, "_maybe_commit",
        lambda *a, **kw: commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, "policy 'next' does not commit"
        ),
    )
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)
    if baseline_sha is not None:
        monkeypatch.setattr(
            pipeline_engine, "_read_baseline_sha", lambda *a, **kw: baseline_sha
        )
    from awf import verify

    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

    import time as _time_mod

    monkeypatch.setattr(
        _time_mod, "monotonic", lambda: float(next(journal["monotonic"]))
    )
    return journal


def _run_worker_stage(project: Path, tmp_path: Path, config: dict | None = None):
    stages = [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute"),
        Stage(name="verify", role="supervisor", kind="verify"),
    ]
    return pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=T, project_dir=project,
        config=config or {}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        pipeline_name=None,
    )


def test_second_retry_reduces_the_pass_and_names_progress(tmp_path, monkeypatch):
    """(b)+(d)+(c): 1-й ретрай — обычный, 2-й — с reduce-строкой и
    путями прогресса; после бюджета — salvage."""
    project, sha = _git_project(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / f"DONE-{T}.md").write_text("# partial draft\n", encoding="utf-8")

    def behavior(run_no, kw):
        (project / "README.md").write_text("worker was here\n", encoding="utf-8")
        holder = kw.get("log_holder")
        if holder is not None and not holder.get("log_path"):
            log_file = project / ".agentic" / "logs" / f"awf-worker-{T}.out"
            log_file.write_text("partial run\n", encoding="utf-8")
            holder["log_path"] = str(log_file)
            holder["log_start_offset"] = 0

    journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
    result = _run_worker_stage(project, tmp_path)

    assert journal["runs"] == 3, "two continue-push retries, then salvage"
    first, second = journal["retry_notes"][1], journal["retry_notes"][2]

    # (b) reduce-строка только на втором ретрае.
    assert "reduce this pass" not in first
    assert "reduce this pass" in second
    assert "run-all" in second

    # (d) путь прогресса — worker-log и DONE-черновик.
    assert f".agentic/logs/awf-worker-{T}.out" in first
    assert f".agentic/outbox/DONE-{T}.md" in first
    assert "do NOT re-read everything" in first

    # (c) бюджет исчерпан → salvage.
    assert result[2] == 1
    state = read_state(project)
    assert state.get("salvage_count") == 1
    assert state.get("sw_retry_count") == 2
    assert (project / ".agentic" / "inbox" / f"SALVAGE-{T}.md").is_file()


def test_inherited_budget_keeps_reduce_on_second_retry(tmp_path, monkeypatch):
    """kill+continue: уже потраченный первый ретрай → следующий несёт
    reduce-строку (счётчик из state), а не начинается заново."""
    project, sha = _git_project(tmp_path)
    write_state(project, sw_retry_count=1, sw_retry_key=f"{T}:worker")

    def behavior(run_no, kw):
        (project / "README.md").write_text("worker was here\n", encoding="utf-8")

    journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
    _run_worker_stage(project, tmp_path)

    note = journal["retry_notes"][1]
    assert "RETRY 2" in note
    assert "reduce this pass" in note
