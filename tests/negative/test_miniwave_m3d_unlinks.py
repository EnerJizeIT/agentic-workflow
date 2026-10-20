"""M3d (TODO-0235): остальные разрушительные ``unlink`` без managed-барьера.

Один класс S9-01: симлинк ``.agentic``/``inbox``/``outbox``/``logs`` уводит
удаление файла за пределы проекта. Инвентарь DONE-0233 (S9-01): восемь
живых мест плюс одно уже закрытое в M3c и одно исключение — девять строк,
у каждой свой вердикт:

1. ``api/pipeline.py::_reconcile`` — барьер ``refuse_unsafe_surfaces``;
2. ``api/pipeline.py::_resolve_pending_closure`` — тот же барьер;
3. ``api/pipeline.py::retry_stage`` — тот же барьер до ``kill_pipeline``;
4. ``pipeline_engine.py::_consume_verify_decision`` — ``assert_no_symlink_escape``;
5. ``pipeline_engine.py`` (потребление сигнала перехода) — то же;
6. ``agent_stage.py`` (чистка stale PROGRESS + сигналов) — то же;
7. ``api/_liveness.py`` (чистка stale PID) — no-follow, отказ удаления;
8. ``api/roles.py`` (draft) — уже закрыт в M3c, тест-подтверждение;
9. ``plan_checkpoint.py`` (stale temp HTML) — исключение: системный temp
   вне ``.agentic``, ``unlink`` листа не идёт по ссылке.

Все проекты — disposable в ``tmp_path``; канарейки лежат ВНЕ проекта.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

T = "TODO-0001"
SURFACES = ("inbox", "outbox", "context", "logs", "done", "state")


def _project(tmp_path: Path, *, git: bool = False) -> Path:
    project = tmp_path / "proj"
    for name in SURFACES:
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "config.yaml").write_text(
        "project: demo\nmodels: {}\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (project / ".agentic" / "roles").mkdir()
    (project / ".agentic" / "roles" / "developer.md").write_text("# dev\n", encoding="utf-8")
    if git:
        import subprocess

        subprocess.run(["git", "init", "-q", str(project)], check=True)
        (project / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=project, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "base"], cwd=project, check=True
        )
    return project


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


def _outside(tmp_path: Path, name: str = "outside") -> Path:
    outside = tmp_path / name
    outside.mkdir()
    (outside / "canary.txt").write_text("keep me", encoding="utf-8")
    return outside


# ─── 1. api/pipeline.py::_reconcile ─────────────────────────────────────


def test_reconcile_refuses_symlinked_inbox(tmp_path):
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (outside / f"ACK-{T}.ready").write_text("", encoding="utf-8")
    (outside / f"APPROVE-{T}.ready").write_text("", encoding="utf-8")
    (outside / f"{T}.md").write_text("# task\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline._reconcile(project)

    assert (outside / f"ACK-{T}.ready").is_file(), "ACK удалён за пределы проекта"
    assert (outside / "canary.txt").read_text(encoding="utf-8") == "keep me"


# ─── 2. api/pipeline.py::_resolve_pending_closure ──────────────────────


def test_resolve_pending_closure_refuses_symlinked_inbox(tmp_path):
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (outside / f"{T}.md").write_text("# task\n", encoding="utf-8")
    signal = outside / f"ACK-{T}.ready"
    signal.write_text("", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline._resolve_pending_closure(project)

    assert signal.is_file(), "решение verify удалено за пределы проекта"


# ─── 3. api/pipeline.py::retry_stage ───────────────────────────────────


def test_retry_stage_refuses_symlinked_inbox(tmp_path, monkeypatch):
    from awf.api import pipeline
    from awf.api._errors import AwfApiError
    from awf.pipeline_state import write_state

    project = _project(tmp_path)
    write_state(
        project,
        todo_id=T,
        salvage_stage="worker",
        logs_dir=project / ".agentic" / "logs",
    )
    outside = _outside(tmp_path)
    salvage = outside / f"SALVAGE-{T}.md"
    salvage.write_text("stale note\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    def _must_not_resume(*_a, **_kw):
        raise AssertionError("retry_stage продолжил запуск на небезопасной поверхности")

    monkeypatch.setattr(pipeline, "continue_pipeline", _must_not_resume)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.retry_stage(project)

    assert salvage.is_file(), "SALVAGE-заметка удалена за пределы проекта"


# ─── 4. pipeline_engine.py::_consume_verify_decision ───────────────────


def test_engine_verify_decision_refuses_symlinked_outbox(tmp_path):
    from awf import pipeline_engine
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    decision = outside / f"REVIEW-{T}.md"
    decision.write_text("reject\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(ManagedPathError, match="symlink"):
        pipeline_engine._consume_verify_decision(
            project, T, f"REVIEW-{T}.md", project / ".agentic" / "logs"
        )

    assert decision.is_file(), "решение verify удалено за пределы проекта"


def test_engine_verify_decision_refuses_symlinked_inbox(tmp_path):
    from awf import pipeline_engine
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    decision = outside / f"APPROVE-{T}.ready"
    decision.write_text("", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(ManagedPathError, match="symlink"):
        pipeline_engine._consume_verify_decision(
            project, T, f"APPROVE-{T}.ready", project / ".agentic" / "logs"
        )

    assert decision.is_file(), "APPROVE удалён за пределы проекта"


# ─── 5. pipeline_engine.py: потребление сигнала перехода ───────────────


def _stages():
    from awf.pipeline import Stage

    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="developer", kind="execute", on_blocked="escalate"),
        Stage(
            name="verify", role="supervisor", kind="verify",
            on_approved="commit_and_next",
        ),
    ]


def _silence_engine(monkeypatch):
    from awf import commit_plan, pipeline_engine, verify

    monkeypatch.setattr(pipeline_engine, "_ensure_baseline_sha", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_engine, "_resolve_prev_handoffs", lambda *a, **k: [])
    monkeypatch.setattr(pipeline_engine, "_run_supervisor_stage", lambda *a, **k: "")
    monkeypatch.setattr(
        pipeline_engine,
        "_maybe_commit",
        lambda *a, **k: commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, "test"
        ),
    )
    monkeypatch.setattr(pipeline_engine, "wait_for_signal", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_engine, "_log", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_engine, "_read_baseline_sha", lambda *a, **k: None)
    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **k: False)


def test_engine_transition_refuses_symlinked_outbox(tmp_path, monkeypatch):
    from awf import pipeline_engine

    project = _project(tmp_path, git=True)
    outside = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "outbox", outside)
    _silence_engine(monkeypatch)
    from awf._log import log as real_log

    monkeypatch.setattr(pipeline_engine, "_log", real_log)

    def mock_agent(stage, todo_id, project_dir, config, logs_dir, **kw):
        (outside / f"DONE-{todo_id}.md").write_text("# done\n", encoding="utf-8")
        (outside / f"DONE-{todo_id}.ready").write_text("", encoding="utf-8")

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", mock_agent)
    stages = _stages()

    result = pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=T, project_dir=project,
        config={}, logs_dir=project / ".agentic" / "logs",
        stages=stages, stage_idx=1, retry_counts=[0, 0, 0],
        auto=False, agent_hard_timeout=None,
    )

    # REVIEW N1: the refusal is a typed stage stop, not a raw exception.
    assert result == (T, 1, 1)
    assert (outside / f"DONE-{T}.ready").is_file(), (
        "сигнал DONE удалён за пределы проекта"
    )


# ─── 6. agent_stage.py: stale PROGRESS и сигналы стадии ────────────────


def test_agent_stage_refuses_symlinked_outbox(tmp_path, monkeypatch):
    from awf import agent_stage
    from awf.infra.managed_fs import ManagedPathError
    from awf.pipeline import Stage

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    stale_progress = outside / f"PROGRESS-{T}.md"
    stale_progress.write_text("old stage notes\n", encoding="utf-8")
    stale_signal = outside / f"DONE-{T}.ready"
    stale_signal.write_text("", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    monkeypatch.setattr(agent_stage, "build_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(agent_stage, "materialize_doctrine", lambda *a, **k: None)

    def _must_not_spawn(*_a, **_kw):
        raise AssertionError("стадия запущена на небезопасной поверхности")

    import awf.signal_watch as signal_watch

    monkeypatch.setattr(signal_watch, "run_subprocess_until_signal", _must_not_spawn)

    stage = Stage(name="worker", role="developer", kind="execute")
    with pytest.raises(ManagedPathError, match="symlink"):
        agent_stage.run_agent_stage(
            stage, T, project, {}, project / ".agentic" / "logs"
        )

    assert stale_progress.is_file(), "PROGRESS удалён за пределы проекта"
    assert stale_signal.is_file(), "сигнал стадии удалён за пределы проекта"


# ─── 7. api/_liveness.py: чистка stale PID ─────────────────────────────


def test_stale_pid_cleanup_skips_symlinked_logs(tmp_path):
    from awf.api import _liveness

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    pid_file = outside / _liveness.PID_FILE_NAME
    pid_file.write_text("999999\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "logs", outside)

    running, pid, source = _liveness.resolve(project)

    assert (running, pid, source) == (False, None, None)
    assert pid_file.is_file(), "stale PID удалён за пределы проекта"


# ─── 8. api/roles.py: draft уже за барьером (M3c) ──────────────────────


def test_role_adopt_refuses_symlinked_draft(tmp_path):
    from awf.api import roles
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    candidate = outside / "auditor.md"
    candidate.write_text("# candidate\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "roles" / "draft", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        roles.adopt_role_draft(project, "auditor")

    assert candidate.read_text(encoding="utf-8") == "# candidate\n"


# ─── 9. plan_checkpoint.py: stale temp HTML — исключение ───────────────


def test_checkpoint_temp_cleanup_is_outside_agentic(tmp_path, monkeypatch):
    import tempfile

    from awf import plan_checkpoint

    project = _project(tmp_path)
    tmp_dir = tmp_path / "system-tmp"
    tmp_dir.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_dir))

    stale = tmp_dir / "awf-checkpoint-old.html"
    stale.write_text("<html></html>", encoding="utf-8")
    old = time.time() - 3600
    os.utime(stale, (old, old))

    assert project not in stale.parents, "вычищаемый temp оказался внутри проекта"

    removed = plan_checkpoint._cleanup_stale_temp_html(project)

    assert removed == 1
    assert not stale.exists()


# ─── N1 (REVIEW виток 2): барьер в стадии — типизированная остановка ───


def test_engine_unsafe_stage_stop_is_logged_and_salvaged(tmp_path, monkeypatch):
    """N1: ``ManagedPathError`` из барьера стадии не уходит «сырым» в cli.

    Отказ барьера — не смерть воркера: ретрай не поможет, но стадия не
    должна исчезнуть молча. Ожидание: строка в orchestrator.log, состояние
    salvage и SALVAGE-заметка в inbox.
    """
    from awf import pipeline_engine
    from awf.infra.managed_fs import ManagedPathError
    from awf.pipeline_state import read_state

    project = _project(tmp_path, git=True)
    outside = _outside(tmp_path)
    _silence_engine(monkeypatch)
    from awf._log import log as real_log

    monkeypatch.setattr(pipeline_engine, "_log", real_log)

    def _refuse(*_a, **_kw):
        raise ManagedPathError(
            f"managed path component is a symlink: {outside}"
        )

    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", _refuse)
    stages = _stages()

    result = pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=T, project_dir=project,
        config={}, logs_dir=project / ".agentic" / "logs",
        stages=stages, stage_idx=1, retry_counts=[0, 0, 0],
        auto=False, agent_hard_timeout=None,
    )

    assert result == (T, 1, 1), "пайплайн должен остановиться, а не продолжать"

    log_text = (project / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "unsafe managed path" in log_text, "отказ барьера не записан в стадию"
    assert "worker" in log_text, "строка стадии не названа"

    state = read_state(project) or {}
    assert state.get("salvage_needed") is True, "salvage-состояние не выставлено"
    assert state.get("salvage_stage") == "worker"
    assert (project / ".agentic" / "inbox" / f"SALVAGE-{T}.md").is_file(), (
        "SALVAGE-заметка не оставлена супервизору"
    )


# ─── N2 (REVIEW виток 2): лист SALVAGE проверяется ДО kill ─────────────


def test_retry_stage_leaf_refusal_has_no_kill_effect(tmp_path, monkeypatch):
    """N2: симлинк-лист SALVAGE → отказ ДО ``kill_pipeline``.

    Комментарий обещает «отказ без эффекта». Проверяем порядок: kill не
    вызван, цель ссылки цела.
    """
    from awf.api import pipeline
    from awf.api._errors import AwfApiError
    from awf.pipeline_state import write_state

    project = _project(tmp_path)
    write_state(
        project,
        todo_id=T,
        salvage_stage="worker",
        logs_dir=project / ".agentic" / "logs",
    )
    outside = _outside(tmp_path)
    target = outside / "salvage-target.md"
    target.write_text("keep me\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / f"SALVAGE-{T}.md").symlink_to(target)

    killed: list[Path] = []
    monkeypatch.setattr(pipeline, "kill_pipeline", lambda p: killed.append(p))

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.retry_stage(project)

    assert killed == [], "kill случился до отказа — «без эффекта» нарушено"
    assert target.read_text(encoding="utf-8") == "keep me\n"


# ─── N3 (REVIEW виток 2): единое правило листа ─────────────────────────


def test_clean_stage_signals_refuses_symlink_leaf(tmp_path):
    """N3: правило единое — симлинк-ЛИСТ сигнала тоже отказ.

    Даже если ``unlink`` сам снял бы ссылку, а не цель: барьер проверяет
    все компоненты, включая лист (Q5).
    """
    from awf.infra.managed_fs import ManagedPathError
    from awf.signals import clean_stage_signals

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    target = outside / "foreign-done.md"
    target.write_text("keep me\n", encoding="utf-8")
    outbox = project / ".agentic" / "outbox"
    (outbox / f"DONE-{T}.ready").symlink_to(target)

    with pytest.raises(ManagedPathError, match="symlink"):
        clean_stage_signals(outbox, T, "DONE")

    assert target.read_text(encoding="utf-8") == "keep me\n"
    assert (outbox / f"DONE-{T}.ready").is_symlink(), "ссылка снята вместо отказа"


# ─── architect-review: один писатель SALVAGE-заметки под барьером ──────


def test_salvage_notes_refuse_symlinked_inbox(tmp_path):
    """Оба писателя SALVAGE-заметки идут через managed-барьер.

    До фикса голый ``atomic_write_text`` уводил заметку за пределы проекта
    при симлинк-``inbox``. Оба входа — тихая остановка стадии и
    salvage-промпт — обязаны отказать и оставить цель целой.
    """
    from awf import pipeline_engine
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    target = outside / f"SALVAGE-{T}.md"
    target.write_text("keep me\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    pipeline_engine._unsafe_stage_stop(
        project, T, "worker", project / ".agentic" / "logs",
        ManagedPathError("managed path component is a symlink: outbox"),
    )
    pipeline_engine._write_salvage_prompt(
        project, T, "worker", None, project / ".agentic" / "logs"
    )

    assert target.read_text(encoding="utf-8") == "keep me\n", (
        "SALVAGE-заметка записана за пределы проекта"
    )


# ─── architect-review: preflight всех листьев ДО первого unlink ────────


def test_transition_consume_checks_all_leaves_before_unlink(tmp_path, monkeypatch):
    """Правило N2 на потреблении сигнала перехода.

    Симлинк на листе ``.md.ready``: прежний порядок успевал снести настоящий
    ``.ready`` и лишь потом отказывал. Ожидание: отказ, оба листа целы.
    """
    from awf import pipeline_engine

    project = _project(tmp_path, git=True)
    outside = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    (outbox / f"DONE-{T}.ready").write_text("", encoding="utf-8")
    target = outside / "foreign-md.ready"
    target.write_text("keep me\n", encoding="utf-8")
    (outbox / f"DONE-{T}.md.ready").symlink_to(target)

    _silence_engine(monkeypatch)
    from awf._log import log as real_log

    monkeypatch.setattr(pipeline_engine, "_log", real_log)
    monkeypatch.setattr(pipeline_engine, "_run_agent_stage", lambda *a, **k: None)
    stages = _stages()

    result = pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=T, project_dir=project,
        config={}, logs_dir=project / ".agentic" / "logs",
        stages=stages, stage_idx=1, retry_counts=[0, 0, 0],
        auto=False, agent_hard_timeout=None,
    )

    assert result == (T, 1, 1), "отказ не остановил стадию"
    assert (outbox / f"DONE-{T}.ready").is_file(), (
        "первый лист снесён до проверки второго"
    )
    assert target.read_text(encoding="utf-8") == "keep me\n", (
        "цель ссылки тронута"
    )


# ─── architect-review: отказ чистки в ветках ретрая — typed stop ───────


def test_retry_cleanup_refusal_is_a_typed_stop(tmp_path, monkeypatch):
    """Одна ветка ретрая — одно поведение: отказ барьера даёт typed stop."""
    from awf import pipeline_engine
    from awf.infra.managed_fs import ManagedPathError
    from awf.pipeline_state import read_state

    project = _project(tmp_path)
    logs = project / ".agentic" / "logs"

    def _refuse(*_a, **_kw):
        raise ManagedPathError("managed path component is a symlink: outbox")

    monkeypatch.setattr(pipeline_engine, "clean_stage_signals", _refuse)

    ok = pipeline_engine._clean_signals_or_stop(
        project, T, "worker", logs, project / ".agentic" / "outbox", ("DONE",)
    )

    assert ok is False, "отказ обязан остановить стадию"
    assert (read_state(project) or {}).get("salvage_needed") is True
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "unsafe managed path" in log_text


# ─── REVIEW виток 3 (B1–B5): интеграция и правило одной поверхности ────


def test_verify_guard_names_actual_stage(tmp_path):
    """B1: typed stop называет фактическую стадию, а не литерал "verify".

    ``awf retry_stage`` ищет salvage-стадию по имени в pipeline: у
    кастомной последней стадии имя ``final-check``, литерал ``verify``
    сделал бы путь восстановления нерабочим.
    """
    from awf import pipeline_engine
    from awf.pipeline_state import read_state

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (outside / f"APPROVE-{T}.ready").write_text("", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    ok = pipeline_engine._consume_verify_decision_guarded(
        project, T, f"APPROVE-{T}.ready",
        project / ".agentic" / "logs", "final-check",
    )

    assert ok is False
    assert (read_state(project) or {}).get("salvage_stage") == "final-check"
    assert (outside / f"APPROVE-{T}.ready").is_file()


def test_closure_refuses_symlinked_outbox(tmp_path):
    """B2: барьер закрытия покрывает outbox, который тут же двигают.

    ``_unblock_todo`` переносит ``BLOCKED-*`` из outbox в context — при
    симлинк-outbox чужие файлы исчезают. Ожидание: отказ до первого
    эффекта, ACK и BLOCKED целы.
    """
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    ack = project / ".agentic" / "inbox" / f"ACK-{T}.ready"
    ack.write_text("", encoding="utf-8")
    (outside / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    (outside / f"BLOCKED-{T}.md").write_text("# blocked\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline._resolve_pending_closure(project)

    assert ack.is_file(), "сигнал закрытия снесён до отказа барьера"
    assert (outside / f"BLOCKED-{T}.ready").is_file()
    assert (outside / f"BLOCKED-{T}.md").is_file()


def test_unblock_todo_preflights_outbox_and_context(tmp_path):
    """B2: у ``_unblock_todo`` свой preflight того же вида.

    Прямой вызов (в обход API-барьера) не должен двигать листья из
    симлинк-outbox: отказ фиксируется в логе, файлы остаются на месте.
    """
    from awf import pipeline_engine

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (outside / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    (outside / f"BLOCKED-{T}.md").write_text("# blocked\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "outbox", outside)
    logs = project / ".agentic" / "logs"

    pipeline_engine._unblock_todo(project, T, logs)

    assert (outside / f"BLOCKED-{T}.ready").is_file()
    assert (outside / f"BLOCKED-{T}.md").is_file()
    assert not (project / ".agentic" / "context" / f"BLOCKED-{T}.ready").exists()
    log_text = (logs / "orchestrator.log").read_text(encoding="utf-8")
    assert "unsafe managed path" in log_text


def test_approve_evidence_unlink_is_guarded(tmp_path):
    """B3: evidence удаляется только после барьера context + листа."""
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    canary = outside / f"CANDIDATE-{T}.json"
    canary.write_text('{"outside": true}\n', encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.approve_commit(project, T, evidence="x", verified_sha="not-a-sha")

    assert canary.is_file(), "чужой CANDIDATE удалён до отказа"


def test_baseline_refuses_symlinked_context(tmp_path):
    """B4: baseline не пишет и не удаляет через симлинк-context."""
    from awf.api import pipeline
    from awf.api._errors import AwfApiError

    project = _project(tmp_path, git=True)
    outside = _outside(tmp_path)
    sha_canary = outside / f"BASELINE-{T}.sha"
    sha_canary.write_text("OUTSIDE CANARY\n", encoding="utf-8")
    snap_canary = outside / f"BASELINE-{T}.worktree"
    snap_canary.write_text("outside worktree\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    with pytest.raises(AwfApiError, match="unsafe managed path"):
        pipeline.create_baseline(project, T)

    assert snap_canary.is_file(), "чужой snapshot удалён"
    assert sha_canary.read_text(encoding="utf-8") == "OUTSIDE CANARY\n"


def test_checkpoint_consume_refuses_symlinked_context(tmp_path):
    """B5: ``os.replace`` решения тоже под барьером — отказ без эффекта."""
    from awf import plan_checkpoint

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    decision = outside / f"CHECKPOINT-{T}.json"
    decision.write_text('{"decision": "approve"}\n', encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "context", outside)

    consumed = plan_checkpoint._consume_checkpoint_decision(
        project, T, project / ".agentic" / "logs"
    )

    assert consumed is False
    assert decision.is_file(), "чужое решение переименовано через симлинк"


def test_agent_stage_checks_progress_leaf_before_signal_cleanup(tmp_path, monkeypatch):
    """Один preflight на оба набора: stale-сигналы и stale PROGRESS.

    До фикса ``clean_stage_signals`` успевал снести настоящий stale-сигнал, а
    отказ приходил уже на симлинк-листе ``PROGRESS``. Правило N2 («все листья
    до первого unlink») обязано держаться и для двух чисток подряд.
    """
    from awf import agent_stage
    from awf.infra.managed_fs import ManagedPathError
    from awf.pipeline import Stage

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    stale_signal = outbox / f"DONE-{T}.ready"
    stale_signal.write_text("", encoding="utf-8")
    target = outside / "progress-target.md"
    target.write_text("keep me\n", encoding="utf-8")
    (outbox / f"PROGRESS-{T}.md").symlink_to(target)

    import awf.signal_watch as signal_watch

    monkeypatch.setattr(agent_stage, "build_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(agent_stage, "materialize_doctrine", lambda *a, **k: None)
    monkeypatch.setattr(
        signal_watch, "run_subprocess_until_signal",
        lambda *a, **k: pytest.fail("стадия запущена после отказа барьера"),
    )

    stage = Stage(name="worker", role="developer", kind="execute")
    with pytest.raises(ManagedPathError, match="symlink"):
        agent_stage.run_agent_stage(
            stage, T, project, {}, project / ".agentic" / "logs"
        )

    assert stale_signal.is_file(), "сигнал снесён до отказа на листе PROGRESS"
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_unblock_todo_preflights_all_leaves_before_first_move(tmp_path):
    """F6: ``_unblock_todo`` проверяет ВСЕ листья до первого переноса.

    Настоящий ``BLOCKED-<id>.ready`` плюс симлинк-лист ``BLOCKED-<id>.md``:
    отказ на листе обязан случиться до первого ``replace`` — иначе набор
    разрезан («a refusal leaves the closure in place» — обещание docstring).
    """
    from awf import pipeline_engine

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    target = outside / "blocked-note.md"
    target.write_text("keep me\n", encoding="utf-8")
    (project / ".agentic" / "outbox" / f"BLOCKED-{T}.md").symlink_to(target)

    pipeline_engine._unblock_todo(project, T, project / ".agentic" / "logs")

    assert (project / ".agentic" / "outbox" / f"BLOCKED-{T}.ready").is_file(), (
        "набор разрезан: .ready уехал в context до отказа на симлинк-листе .md"
    )
    assert not (project / ".agentic" / "context" / f"BLOCKED-{T}.ready").exists()
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_net_death_refusal_does_not_spend_retry_budget(tmp_path):
    """F7: отказ чистки не тратит сетевой ретрай и не пишет state.

    Ретрай отменён отказом — бюджет не инкрементируется, ``net_retry_count``
    не попадает в state (иначе он переживает kill+continue и съедает ретрай).
    """
    from awf import pipeline_engine
    from awf.pipeline_state import read_state

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    retries, scheduled = pipeline_engine._handle_net_death(
        s_name="worker", net_retries=0, net_limit=2, net_key=f"{T}:worker",
        project_dir=project, logs_dir=project / ".agentic" / "logs",
        outbox=project / ".agentic" / "outbox",
        todo_id=T, prefixes=("DONE",),
    )

    assert scheduled is False
    assert retries == 0, "бюджет потрачен, хотя ретрай отменён отказом чистки"
    state = read_state(project) or {}
    assert not state.get("net_retry_count"), (
        f"net_retry_count={state.get('net_retry_count')!r} переживёт kill+continue"
    )


# ─── QA (attempt 2): dst-лист, битый симлинк, цена отказа F7 ───────────


def test_unblock_todo_checks_context_destination_leaf(tmp_path):
    """F6 (dst-сторона): симлинк-лист НАЗНАЧЕНИЯ в context — тоже отказ.

    Пара ``(src, dst)`` проверяется целиком: настоящие ``.ready`` и ``.md``
    в outbox не должны уехать, если ``context/BLOCKED-<id>.md`` — ссылка
    наружу. Иначе перенос уничтожил бы ссылку на месте, а ``.ready`` уже
    лежал бы в context — тот же разрезанный набор.
    """
    from awf import pipeline_engine

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    ctx = project / ".agentic" / "context"
    (outbox / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{T}.md").write_text("# blocked\n", encoding="utf-8")
    canary = outside / "ctx-note.md"
    canary.write_text("keep me\n", encoding="utf-8")
    (ctx / f"BLOCKED-{T}.md").symlink_to(canary)

    pipeline_engine._unblock_todo(project, T, project / ".agentic" / "logs")

    assert (outbox / f"BLOCKED-{T}.ready").is_file(), "набор разрезан на dst-отказе"
    assert (outbox / f"BLOCKED-{T}.md").is_file()
    assert not (ctx / f"BLOCKED-{T}.ready").exists()
    assert canary.read_text(encoding="utf-8") == "keep me\n"


def test_unblock_todo_treats_broken_symlink_leaf_as_leaf(tmp_path):
    """F6: битый симлинк-лист входит в набор (``exists()`` для него False).

    Сбор пар идёт по ``is_symlink() or exists()``; пропуск битой ссылки
    означал бы, что ``.ready`` уехал, а отказ пришёл позже.
    """
    from awf import pipeline_engine

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    outbox = project / ".agentic" / "outbox"
    ctx = project / ".agentic" / "context"
    (outbox / f"BLOCKED-{T}.ready").write_text("", encoding="utf-8")
    (outbox / f"BLOCKED-{T}.md").symlink_to(outside / "missing.md")

    pipeline_engine._unblock_todo(project, T, project / ".agentic" / "logs")

    assert (outbox / f"BLOCKED-{T}.ready").is_file()
    assert (outbox / f"BLOCKED-{T}.md").is_symlink(), "битая ссылка снята"
    assert not (ctx / f"BLOCKED-{T}.ready").exists()


def test_net_death_refusal_does_not_sleep_or_print_retry(
    tmp_path, monkeypatch, capsys
):
    """F7: отказ чистки не уходит в backoff и не печатает «retrying».

    Бюджет и state проверены отдельной пробой; здесь — вторая половина
    обещания: отказ не должен тратить время на сон и врать в консоли.
    """
    from awf import pipeline_engine

    project = _project(tmp_path)
    outside = _outside(tmp_path)
    _replace_with_symlink(project / ".agentic" / "outbox", outside)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    retries, scheduled = pipeline_engine._handle_net_death(
        s_name="worker", net_retries=0, net_limit=2, net_key=f"{T}:worker",
        project_dir=project, logs_dir=project / ".agentic" / "logs",
        outbox=project / ".agentic" / "outbox",
        todo_id=T, prefixes=("DONE",),
    )

    assert (retries, scheduled) == (0, False)
    assert sleeps == [], "backoff отработал, хотя ретрай отменён отказом"
    assert "retrying" not in capsys.readouterr().err.lower()
