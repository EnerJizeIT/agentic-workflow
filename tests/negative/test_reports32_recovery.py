"""REPORTS32 (TODO-0196): recovery — stale stage-state в run_finish,
continue-push ретрай no-signal с работой, zombie-подсказка в статусе.

Источник: репорт awf-bug-20261011-recovery-i-commit-gate-chastichnye-
kommity-yunitov-stale.md (jql, 1.7.0):
- (2) `awf_run_finish` дважды отказал «pipeline dead, stage state NOT
  cleared», хотя юнит заархивирован; закрытие проделывалось
  python-обходом `pipeline_state.clear_state()`;
- (3) TODO-0064: воркер закончил ход вопросом и вышел БЕЗ сигнала DONE
  (артефакты — тесты и фикстура — на месте), движок <defunct>, авторетрая
  не было ~7 минут, ран стоял до ручного `awf_continue`.

Инварианты (приёмка a–d):
- (a) archived + мёртвый пайплайн + stage-state висит → run_finish
  закрывает забег, state очищен, RUN-REPORT фиксирует сброс;
- (b) юнит не заархивирован → отказ как раньше, state не тронут;
- (c) silent-выход воркера С работой → continue-push ретрай (не
  мгновенный salvage), после бюджета (`retry.silent_work_attempts`,
  default 1) — salvage;
- (d) мёртвый движок + stale stage-state + сигналов нет → `awf_status`
  и `awf_brief` показывают zombie-подсказку с `awf_continue`.

Hermetic: живость — шов `_liveness.resolve` (без реальных процессов),
воркер — сценарный `_run_agent_stage`, репо — `_git_init` из conftest.
"""
from __future__ import annotations

from pathlib import Path

from awf import api, commit_plan, pipeline_engine, run_state
from awf.api import _liveness
from awf.pipeline import Stage
from awf.pipeline_state import read_state, write_state

T = "TODO-0001"
T2 = "TODO-0002"


# ─── (a)+(b): run_finish и stale stage-state ─────────────────────────────


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports32Recovery")
    return tmp_git_repo


def _active_run_with_current(proj: Path) -> None:
    """Активный забег с установленным current (как после run_next)."""
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{T}.md").write_text("# Task\n", encoding="utf-8")
    api.run_start(proj, queue=[T])
    run_state.write_run(proj, current=T, index=1)


def _parked_stage(proj: Path, stage: str = "verify", todo_id: str = T) -> None:
    """Стадия не очищена: пайплайн умер, state-файл лежит."""
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    write_state(
        proj, stage_name=stage, stage_kind=stage, todo_id=todo_id, logs_dir=logs
    )


def _archive(proj: Path, todo_id: str = T) -> None:
    done = proj / ".agentic" / "done" / todo_id
    done.mkdir(parents=True, exist_ok=True)
    (done / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")


def _reports(proj: Path) -> list[Path]:
    outbox = proj / ".agentic" / "outbox"
    return sorted(outbox.glob("RUN-REPORT-*.md")) if outbox.is_dir() else []


class TestRunFinishStaleState:
    def test_archived_dead_stale_state_closes_and_records(self, tmp_git_repo):
        """(a) юнит заархивирован, пайплайн мёртв, stage-state висит →
        run_finish закрывает забег, очищает state и фиксирует сброс
        в RUN-REPORT."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)
        _parked_stage(proj)

        result = api.run_finish(proj)

        assert result.active is False, "the stale state must not block the close"
        assert result.report_file
        assert read_state(proj) is None, "the stale stage state must be cleared"
        text = _reports(proj)[-1].read_text(encoding="utf-8")
        assert "stale stage-state cleared" in text
        assert T in text
        assert "verify" in text

    def test_unarchived_stale_state_refused(self, tmp_git_repo):
        """(b) юнит не заархивирован → отказ как раньше (REPORTS29):
        stage-state — точка возобновления, не трогать."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _parked_stage(proj)

        result = api.run_finish(proj)

        assert result.active is True
        assert _reports(proj) == []
        assert "refused" in result.message
        assert read_state(proj) is not None

    def test_alive_pipeline_state_not_cleared(self, tmp_git_repo, monkeypatch):
        """Пайплайн жив → отказ (running); state не чистить — движок им
        ещё пользуется."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)
        _parked_stage(proj)
        monkeypatch.setattr(
            _liveness, "resolve", lambda project_dir: (True, 4321, "test")
        )

        result = api.run_finish(proj)

        assert result.active is True
        assert read_state(proj) is not None

    def test_foreign_unit_state_not_cleared(self, tmp_git_repo):
        """State чужого юнита — не наш stale: отказ, state не тронут."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)
        _parked_stage(proj, todo_id=T2)

        result = api.run_finish(proj)

        assert result.active is True
        state = read_state(proj)
        assert state is not None and state["todo_id"] == T2


# ─── (c): silent-выход с работой → continue-push ретрай ─────────────────


def _stages() -> list[Stage]:
    return [
        Stage(name="plan", role="supervisor", kind="plan"),
        Stage(name="worker", role="worker", kind="execute", on_blocked="escalate"),
        Stage(name="verify", role="supervisor", kind="verify", on_approved="commit_and_next"),
    ]


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

    import time as _time_mod

    monkeypatch.setattr(_time_mod, "monotonic", lambda: float(next(journal["monotonic"])))

    from awf import verify

    monkeypatch.setattr(verify, "attempt_auto_done", lambda *a, **kw: False)

    return journal


def _run_worker_stage(project: Path, tmp_path: Path, config: dict | None = None):
    stages = _stages()
    return pipeline_engine.execute_agent_stage(
        stage=stages[1], current_todo=T, project_dir=project,
        config=config or {}, logs_dir=tmp_path, stages=stages, stage_idx=1,
        retry_counts=[0, 0, 0], auto=False, agent_hard_timeout=None,
        pipeline_name=None,
    )


class TestSilentWorkContinuePush:
    def test_silent_with_work_retry_then_salvage(self, tmp_path, monkeypatch):
        """(c) нет сигнала, но работа в дереве → continue-push ретраи
        (не мгновенный salvage), после бюджета — salvage. TODO-0226:
        дефолт бюджета 2, второй ретрай несёт reduce-строку."""
        project, sha = _git_project(tmp_path)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")

        journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
        result = _run_worker_stage(project, tmp_path)

        assert journal["runs"] == 3, "two continue-push retries before salvage"
        assert journal["retry_notes"][0] is None
        note = journal["retry_notes"][1]
        assert "NO signal" in note
        assert "Do NOT redo" in note
        assert "reduce this pass" in journal["retry_notes"][2]
        assert result[2] == 1
        state = read_state(project)
        assert state.get("salvage_count") == 1
        assert state.get("sw_retry_count") == 2

    def test_silent_with_work_retry_finishes_with_signal(self, tmp_path, monkeypatch):
        """Ретрай доводит работу и завершает сигналом → переход,
        без salvage."""
        project, sha = _git_project(tmp_path)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")
            if run_no == 2:
                outbox = project / ".agentic" / "outbox"
                (outbox / f"DONE-{T}.ready").write_text("", encoding="utf-8")
                (outbox / f"DONE-{T}.md").write_text("# Done\n", encoding="utf-8")

        journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
        result = _run_worker_stage(project, tmp_path)

        assert journal["runs"] == 2
        assert result[2] == 0, "the retry's signal must drive the transition"
        assert not read_state(project).get("salvage_needed")

    def test_silent_with_work_budget_zero_salvages_immediately(self, tmp_path, monkeypatch):
        """`retry.silent_work_attempts: 0` отключает ретрай — salvage
        сразу (config-ключ — рычаг)."""
        project, sha = _git_project(tmp_path)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")

        journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
        result = _run_worker_stage(
            project, tmp_path,
            config={"retry": {"silent_work_attempts": 0}},
        )

        assert journal["runs"] == 1
        assert result[2] == 1
        assert read_state(project).get("salvage_count") == 1

    def test_silent_with_work_budget_survives_restart(self, tmp_path, monkeypatch):
        """kill+continue: потраченный бюджет не передают — счётчик из
        state по ключу (todo_id, stage) наследуется, а не сбрасывается
        (тот же паттерн, что у net-счётчика, AUD02-07). Бюджет 2, один
        уже потрачен → ровно ОДИН следующий continue-push ретрай, а не
        два свежих."""
        project, sha = _git_project(tmp_path)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")

        journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
        # Стадия уже потратила один ретрай (1/2) — как после
        # kill+continue: счётчик записан в state.
        write_state(project, sw_retry_count=1, sw_retry_key=f"{T}:worker")
        result = _run_worker_stage(
            project, tmp_path,
            config={"retry": {"silent_work_attempts": 2}},
        )

        assert journal["runs"] == 2, (
            "the inherited budget must not be reissued: one spent, one left"
        )
        note = journal["retry_notes"][1]
        assert "left WORK in the tree but NO signal" in note
        assert result[2] == 1
        state = read_state(project)
        assert state.get("salvage_count") == 1
        assert state.get("sw_retry_count") == 2

    def test_silent_with_work_budget_two_exhausts_then_salvage(self, tmp_path, monkeypatch):
        """Бюджет 2 → два continue-push ретрая (твёрдый предел — attempt
        loop), затем salvage."""
        project, sha = _git_project(tmp_path)

        def behavior(run_no, kw):
            (project / "README.md").write_text("worker was here\n", encoding="utf-8")

        journal = _mock_worker(monkeypatch, behavior, baseline_sha=sha)
        result = _run_worker_stage(
            project, tmp_path,
            config={"retry": {"silent_work_attempts": 2}},
        )

        assert journal["runs"] == 3
        assert result[2] == 1
        state = read_state(project)
        assert state.get("salvage_count") == 1
        assert state.get("sw_retry_count") == 2


# ─── (d): zombie-подсказка в статусе и карточке ──────────────────────────


class TestZombieStateHint:
    def test_status_shows_zombie_hint(self, tmp_git_repo):
        """(d) мёртвый движок + stage-state + сигналов нет → подсказка
        с точным продолжением (awf_continue)."""
        proj = _project(tmp_git_repo)
        _parked_stage(proj)

        result = api.get_status(proj)

        assert result.pipeline_running is False
        assert result.zombie_state, "the zombie must be named in the status"
        assert "awf_continue" in result.zombie_state
        assert T in result.zombie_state

    def test_status_no_zombie_when_signal_lies(self, tmp_git_repo):
        """DONE-сигнал лежит → дело за approve, а не за continue —
        подсказки нет."""
        proj = _project(tmp_git_repo)
        _parked_stage(proj)
        outbox = proj / ".agentic" / "outbox"
        (outbox / f"DONE-{T}.ready").write_text("", encoding="utf-8")
        (outbox / f"DONE-{T}.md").write_text("# Done\n", encoding="utf-8")

        result = api.get_status(proj)

        assert result.zombie_state is None

    def test_status_no_zombie_when_salvage_pending(self, tmp_git_repo):
        """Salvage уже объявлен → своя поверхность (expected_action),
        zombie-подсказка не дублирует."""
        proj = _project(tmp_git_repo)
        logs = proj / ".agentic" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        write_state(
            proj,
            stage_name="worker", stage_kind="execute", todo_id=T,
            salvage_needed=True, salvage_stage="worker", salvage_count=1,
            logs_dir=logs,
        )

        result = api.get_status(proj)

        assert result.salvage_needed is True
        assert result.zombie_state is None

    def test_status_no_zombie_when_pipeline_running(self, tmp_git_repo, monkeypatch):
        """Пайплайн жив — state в норме, не zombie."""
        proj = _project(tmp_git_repo)
        _parked_stage(proj)
        from awf.api import lifecycle

        monkeypatch.setattr(
            lifecycle, "check_pipeline_running", lambda *a, **kw: (True, 4321, "")
        )

        result = api.get_status(proj)

        assert result.pipeline_running is True
        assert result.zombie_state is None

    def test_brief_card_carries_zombie_line(self, tmp_git_repo):
        """Карточка awf_brief показывает ту же подсказку в State."""
        proj = _project(tmp_git_repo)
        _parked_stage(proj)

        result = api.brief(proj)

        assert result.zombie_state
        assert "zombie-state:" in result.text
        assert T in result.text
