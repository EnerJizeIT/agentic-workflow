"""REPORTS26 B3 (TODO-0158): done после approve — цикл без коммита не
теряет событие.

Инцидент (репорт awf-bug-20261007-posle-approve-sobytie-done-prihodit-ne-
vsegda-payplayn.md): в забеге юнит без диффа (verify-коммит пропущен) →
после approve ``wait_for_event`` вернул ``idle`` ("stage state cleared")
вместо ``done``; ``run_status`` показывал current TODO, completed [], хотя
юнит уже в done/ и закоммичен. Состояние догналось только на следующем
``awf_run_next``.

Диагноз: ``_last_completed_todo`` предпочитает НОВЕЙШИЙ awf-коммит архиву.
В цикле без коммита новейший коммит принадлежит ПРЕДЫДУЩЕМУ юниту, а
архив — текущему → сверка "evidence == run.current" падает → ``done`` не
отдаётся.

Инварианты:
1. Внутри АКТИВНОГО забега закрытием current-юнита считается его АРХИВ
   (архивирован и не активен снова — определение завершённости гейтов
   забега), независимо от того, чей коммит новейший. Фуза RUN10 #1
   (мёртвый пайплайн без завершённого current → не ``done``) сохраняется.
2. Вне забега — поведение прежнее (newest evidence:
   commit+archive/commit/archive).
3. run_brief/run_status при ЧТЕНИИ кредитуют завершённый current в
   completed — не двигая index/current (их двигает run_next).
"""
from __future__ import annotations

import subprocess
import time as _time
from pathlib import Path

from awf import api, run_state
from awf.pipeline_state import clear_state, write_state

T1 = "TODO-0001"
T2 = "TODO-0002"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports26B3")
    return tmp_git_repo


def _commit_awf(proj: Path, todo_id: str) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", f"awf(verify): {todo_id}"],
        cwd=proj,
        check=True,
    )


def _archive(proj: Path, todo_id: str) -> None:
    d = proj / ".agentic" / "done" / todo_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "TODO.md").write_text(f"# {todo_id}\n", encoding="utf-8")


def _clean_exit_leftover(proj: Path) -> None:
    """Чистый выход оркестратора: маркеры стадий убраны, phase=done."""
    clear_state(proj)
    write_state(proj, phase="done", goal="g", normalized=True)


def _no_commit_cycle(proj: Path) -> None:
    """Забег, где текущий юнит завершился БЕЗ коммита:

    - TODO-0001 (предыдущий юнит) закоммичен и заархивирован,
    - TODO-0002 (current) заархивирован, но коммита НЕТ — дифф был пуст,
      verify-коммит пропущен;
    - run.yaml: current=TODO-0002, completed=[TODO-0001] (кредит предыдущего
      юнита сделан при запуске текущего).
    """
    _commit_awf(proj, T1)
    _archive(proj, T1)
    _archive(proj, T2)
    run_state.write_run(
        proj,
        queue=[T1, T2],
        index=2,
        current=T2,
        completed=[T1],
        active=True,
    )


class TestDoneAfterNoCommitCycle:
    def test_done_fires_for_archived_current(self, tmp_git_repo):
        """(a) Активный забег, current заархивирован без коммита,
        новейший коммит — предыдущего юнита, state phase=done, процесс
        мёртв → ``done`` называет current и ведёт к awf_run_next.
        Красный на baseline: ``idle`` ("stage state cleared")."""
        proj = _project(tmp_git_repo)
        _no_commit_cycle(proj)
        _clean_exit_leftover(proj)

        t0 = _time.monotonic()
        result = api.wait_for_event(proj, timeout=5, poll_interval=1)
        elapsed = _time.monotonic() - t0

        assert result.event_type == "done"
        assert T2 in result.message
        assert "awf_run_next" in result.message
        assert elapsed < 1.0, "done must come from the initial check"

    def test_run_surfaces_credit_finished_current_on_read(self, tmp_git_repo):
        """(b) run_brief/run_status: completed содержит завершённый
        current (read-time кредит). Красный на baseline: completed =
        [TODO-0001] без TODO-0002. index/current при этом НЕ двигаются —
        это работа run_next; run.yaml чтение не трогает."""
        proj = _project(tmp_git_repo)
        _no_commit_cycle(proj)

        status = api.run_status(proj)
        assert T2 in status.completed
        assert T1 in status.completed
        assert status.current == T2
        assert status.index == 2

        brief = api.run_brief(proj)
        assert T2 in brief["completed"]
        assert T1 in brief["completed"]

        state = run_state.read_run(proj)
        assert state["completed"] == [T1]
        assert state["index"] == 2
        assert state["current"] == T2

    def test_fuse_current_not_finished_is_not_done(self, tmp_git_repo):
        """(c) Контроль-фуза RUN10 #1: current НЕ заархивирован
        (пайплайн умер в середине итерации) → не ``done`` (idle/timeout),
        как раньше — архив current, а не коммит, является знаком
        завершённости в забеге."""
        proj = _project(tmp_git_repo)
        _commit_awf(proj, T1)
        _archive(proj, T1)
        # done/TODO-0002/ НЕТ — current не завершён.
        run_state.write_run(
            proj,
            queue=[T1, T2],
            index=2,
            current=T2,
            completed=[T1],
            active=True,
        )
        _clean_exit_leftover(proj)

        result = api.wait_for_event(proj, timeout=2, poll_interval=1)

        assert result.event_type in ("idle", "timeout")
        assert result.event_type != "done"
        assert T1 not in result.message
