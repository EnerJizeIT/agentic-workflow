"""REPORTS29 (TODO-0170): run_finish не закрывает забег при незавершённом юните.

Инцидент (репорт awf-bug-20261009-run-finish-zakryvaet-ran-kogda-yunit-ne-
zavershen.md, проект jql, забег 1.4.0, TODO-0027): пайплайн умер на verify
после approve; `run_finish` закрыл забег «8/8 approved», хотя 8-й юнит
не закоммичен и не архивирован — ложное «всё готово» в главном артефакте
владельца (RUN-REPORT).

Инварианты:
- (a) активный забег + current не заархивирован → отказ: забег остаётся
  active, RUN-REPORT не пишется;
- (b) пайплайн мёртв + стадия не очищена + сигнал лежит → отказ
  перечисляет факты (pid, стадия, сигнал, дерево) и что делать;
- (c) force=True → закрывает, RUN-REPORT несёт пометку
  «closed with unfinished unit TODO-NNNN (force)»;
- (d) current заархивирован + стадия очищена → штатное закрытие;
  отчёт содержит факты (архив, пайплайн мёртв/жив).

Hermetic: синтез run-state напрямую (write_run), живость пайплайна —
через шов _liveness.resolve, реального процесса нет.
"""
from __future__ import annotations

from pathlib import Path

from awf import api, run_state
from awf.api import _liveness
from awf.pipeline_state import write_state

T = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports29Finish")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = T) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")


def _active_run_with_current(proj: Path) -> None:
    """Активный забег с установленным current (как после run_next)."""
    _write_todo(proj)
    api.run_start(proj, queue=[T])
    run_state.write_run(proj, current=T, index=1)


def _parked_stage(proj: Path, stage: str = "verify") -> None:
    """Стадия не очищена: пайплайн умер, state-файл лежит (как после
    убитого verify-ожидания)."""
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    write_state(
        proj,
        stage_name=stage,
        stage_kind=stage,
        todo_id=T,
        logs_dir=logs,
    )


def _archive(proj: Path, todo_id: str = T) -> None:
    done = proj / ".agentic" / "done" / todo_id
    done.mkdir(parents=True, exist_ok=True)
    (done / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")


def _signal_approve(proj: Path, todo_id: str = T) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"APPROVE-{todo_id}.ready").touch()


def _reports(proj: Path) -> list[Path]:
    outbox = proj / ".agentic" / "outbox"
    return sorted(outbox.glob("RUN-REPORT-*.md")) if outbox.is_dir() else []


def _latest_report_text(proj: Path) -> str:
    reports = _reports(proj)
    assert reports, "no RUN-REPORT was written"
    return reports[-1].read_text(encoding="utf-8")


def _live_pipeline(monkeypatch, pid: int = 4321) -> None:
    monkeypatch.setattr(
        _liveness, "resolve", lambda project_dir: (True, pid, "test")
    )


class TestRefusal:
    """(a)+(b): незавершённый current — отказ, забег жив, факты в тексте."""

    def test_unarchived_current_refused(self, tmp_git_repo):
        """(a) активный забег, current не заархивирован → отказ: забег
        остаётся active, RUN-REPORT не пишется (никакого «8/8»)."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)

        result = api.run_finish(proj)

        assert result.active is True, "the run must stay active"
        assert result.report_file == ""
        assert _reports(proj) == [], "no RUN-REPORT on a refused close"
        state = run_state.read_run(proj)
        assert state["active"] is True
        assert state["current"] == T
        assert "refused" in result.message
        assert f"NOT archived (done/{T}/TODO.md absent)" in result.message

    def test_dead_pipeline_stage_and_signal_refusal_names_facts(
        self, tmp_git_repo
    ):
        """(b) пайплайн мёртв, стадия не очищена, APPROVE лежит → отказ
        перечисляет факты и действия (continue/approve)."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _parked_stage(proj)
        _signal_approve(proj)

        result = api.run_finish(proj)

        assert result.active is True
        assert _reports(proj) == []
        msg = result.message
        assert "- pipeline: dead (no live pid)" in msg
        assert f"- stage state: NOT cleared (verify for {T})" in msg
        assert f"- unit: NOT archived (done/{T}/TODO.md absent)" in msg
        assert f".agentic/inbox/APPROVE-{T}.ready" in msg
        assert "awf_continue" in msg, "the dead stage must be resumable"
        assert "awf_approve" in msg, "the lying APPROVE must be named"
        assert run_state.read_run(proj)["active"] is True

    def test_alive_pipeline_refused_even_if_archived(self, tmp_git_repo, monkeypatch):
        """Invariant 1(ii): пайплайн жив → отказ, даже если юнит уже
        заархивирован; отказ называет pid."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)
        _live_pipeline(monkeypatch)

        result = api.run_finish(proj)

        assert result.active is True
        assert _reports(proj) == []
        assert "- pipeline: ALIVE (pid 4321)" in result.message
        assert "awf_kill" in result.message


class TestForce:
    """(c): force=True — осознанное закрытие с потерей, пометка в отчёте."""

    def test_force_closes_and_marks_report(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _parked_stage(proj)

        result = api.run_finish(proj, force=True)

        assert result.active is False
        assert result.report_file, "the force close writes the report"
        state = run_state.read_run(proj)
        assert state["active"] is False
        text = _latest_report_text(proj)
        assert (
            f"closed with unfinished unit {T} (force)" in text
        ), f"the force mark is missing: {text}"
        assert f"unit {T} NOT archived" in text

    def test_force_not_needed_for_fresh_unit(self, tmp_git_repo):
        """force=True при завершённом юните — пометки «с потерей» нет:
        потеря не была."""
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)

        result = api.run_finish(proj, force=True)

        assert result.active is False
        text = _latest_report_text(proj)
        assert "closed with unfinished unit" not in text


class TestNormalClose:
    """(d): завершённый current — штатное закрытие, факты в отчёте."""

    def test_archived_current_closes_normally(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _active_run_with_current(proj)
        _archive(proj)

        result = api.run_finish(proj)

        assert result.active is False
        assert result.report_file
        state = run_state.read_run(proj)
        assert state["active"] is False
        text = _latest_report_text(proj)
        assert f"{T} archived (done/{T}/TODO.md)" in text
        assert "**Pipeline:** not running" in text

    def test_no_current_closes_normally(self, tmp_git_repo):
        """Инвариант 4: активный забег без current — как раньше."""
        proj = _project(tmp_git_repo)
        api.run_start(proj, queue=[T])

        result = api.run_finish(proj)

        assert result.active is False
        assert result.report_file
        assert not _latest_report_text(proj).count("**Unit:**")

    def test_no_run_state(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        result = api.run_finish(proj)
        assert result.active is False
        assert "No run state found" in result.message
        assert _reports(proj) == []
