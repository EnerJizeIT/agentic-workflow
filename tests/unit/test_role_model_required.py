"""A48: a role without a model must not silently run on a foreign default.

The incident (04.11): ``models.supervisor`` had only ``description``. The
engine passed no ``--model``, opencode took its own default (``vllm/llm``,
VPN-only) and replan sessions died with ECONNRESET.

This file is the verify contract: resolution priority and the refusal at
every spawn surface. The worker-fallback argv and the doctor rows live in
``test_role_model_inheritance.py`` and ``test_role_model_doctor.py``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.agent_stage import run_agent_stage
from awf.pipeline import Stage
from awf.supervisor import (
    RoleModelMissingError,
    get_role_model,
    require_role_model,
    run_supervisor_stage,
)
from tests.unit._role_model_fixtures import WORKER_MODEL, FakePopen, patch_spawn

# ─── resolution: role → worker fallback → refusal ──────────────────────────


def test_role_model_uses_worker_fallback() -> None:
    config = {
        "models": {
            "supervisor": {"description": "no model here"},
            "worker": {"model": WORKER_MODEL},
        }
    }
    assert get_role_model(config, "supervisor") == WORKER_MODEL
    assert require_role_model(config, "supervisor") == WORKER_MODEL


def test_own_model_wins_over_worker_fallback() -> None:
    config = {
        "models": {
            "supervisor": {"model": "p/own"},
            "worker": {"model": WORKER_MODEL},
        }
    }
    assert get_role_model(config, "supervisor") == "p/own"


def test_require_role_model_names_the_role() -> None:
    config = {"models": {"supervisor": {"description": "only a description"}}}
    with pytest.raises(RoleModelMissingError) as exc:
        require_role_model(config, "supervisor")
    assert "supervisor" in str(exc.value)
    assert "models.supervisor.model" in str(exc.value)


# ─── refusal before the spawn: agent stage ─────────────────────────────────


def test_agent_spawn_refuses_without_any_model(
    role_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = role_project({"models": {"worker": {"description": "no model"}}})
    patch_spawn(monkeypatch)

    stage = Stage(name="execute", role="worker", kind="execute")
    with pytest.raises(RoleModelMissingError) as exc:
        run_agent_stage(
            stage, "TODO-0001", project_dir,
            yaml.safe_load((project_dir / ".agentic" / "config.yaml").read_text()),
            project_dir / ".agentic" / "logs",
        )

    assert "worker" in str(exc.value)
    assert FakePopen._last_cmds == [], "no process may exist without a model"


# ─── refusal before the spawn: supervisor / replan ─────────────────────────


def test_supervisor_spawn_refuses_without_any_model(
    role_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = role_project({
        "models": {"supervisor": {"description": "no model"}}
    })
    patch_spawn(monkeypatch)

    stage = Stage(name="replan", role="supervisor", kind="replan")
    with pytest.raises(RoleModelMissingError) as exc:
        run_supervisor_stage(
            stage, todo_id="TODO-0001", auto=True, project_dir=project_dir,
            logs_dir=project_dir / ".agentic" / "logs",
        )

    assert "supervisor" in str(exc.value)
    assert FakePopen._last_cmds == [], "no process may exist without a model"


def test_skipped_supervisor_stage_still_skips_without_a_model(
    role_project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A48: the model guard belongs to the spawn, not to the dispatch.

    ``salvage`` / an unknown kind spawn nothing — the documented "empty
    string if skipped" must not turn into a refusal just because the
    project has no supervisor model.
    """
    project_dir = role_project({
        "models": {"supervisor": {"description": "no model at all"}}
    })
    patch_spawn(monkeypatch)
    config = yaml.safe_load(
        (project_dir / ".agentic" / "config.yaml").read_text(encoding="utf-8")
    )

    from awf.supervisor import run_supervisor_via_subprocess

    for kind in ("salvage", "bogus"):
        assert run_supervisor_via_subprocess(
            kind, "", project_dir, config,
            ".agentic/phases/plan.md", project_dir / ".agentic" / "logs",
        ) == ""
    assert FakePopen._last_cmds == [], "a skipped stage spawns nothing"


# ─── engine: a refusal is not a crashed stage ──────────────────────────────


def test_engine_reports_missing_model_as_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A48: ``RoleModelMissingError`` is a RuntimeError subclass, so the
    generic crash branch would hide the cause behind "agent stage crashed".
    The engine must name the role and stop — once, without a retry."""
    from awf import pipeline_engine
    from awf.pipeline import Stage as PipelineStage

    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)

    attempts: list[str] = []

    def refuse(*_a, **_kw):
        attempts.append("spawn")
        raise RoleModelMissingError(
            "у роли worker нет модели, добавьте models.worker.model "
            "(общая модель проекта)"
        )

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", refuse)
    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    stages = [
        PipelineStage(name="plan", role="supervisor", kind="plan"),
        PipelineStage(name="implement", role="worker", kind="execute"),
        PipelineStage(name="verify", role="supervisor", kind="verify"),
    ]
    todo, _idx, rc = pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo="TODO-0001", project_dir=project,
        config={}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=True, agent_hard_timeout=None,
    )

    err = capsys.readouterr().err
    assert rc == 1, "a missing model must stop the pipeline"
    assert todo == "TODO-0001"
    assert "у роли worker нет модели" in err, err
    assert "crashed" not in err, (
        "the refusal must not be reported as a crashed stage:\n" + err
    )
    assert attempts == ["spawn"], "one attempt, no silent retry"


def test_supervisor_stage_reports_missing_model_as_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A48: the supervisor spawn surface has the same branch.

    ``_stop_on_missing_model`` (architect F1) replaced the generic
    "supervisor stage crashed" — a broken config must read as a refusal,
    not as a stage that fell over.
    """
    from awf import pipeline_engine
    from awf.pipeline import Stage as PipelineStage

    project = tmp_path / "proj"
    (project / ".agentic" / "outbox").mkdir(parents=True)

    def refuse(*_a, **_kw):
        raise RoleModelMissingError(
            "у роли supervisor нет модели, добавьте models.supervisor.model "
            "(или models.worker.model с общей моделью проекта)"
        )

    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", refuse)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **kw: None)

    stage = PipelineStage(name="plan", role="supervisor", kind="plan")
    todo, delta, rc = pipeline_engine.execute_supervisor_stage(
        stage=stage, current_todo="", auto=True, project_dir=project,
        config={}, logs_dir=tmp_path,
    )

    err = capsys.readouterr().err
    assert rc == 1, "a missing model must stop the pipeline"
    assert delta == 0 and todo == ""
    assert "у роли supervisor нет модели" in err, err
    assert "crashed" not in err, (
        "the refusal must not be reported as a crashed supervisor stage:\n"
        + err
    )
