"""REPORTS32 (TODO-0197): события и статус не врут.

Инциденты (репорт awf-bug-20261011-recovery-i-commit-gate-
chastichnye-kommity-yunitov-stale п.4 + фича-репорт
awf-feature-20261011-prove-red-yunit-pokrytie п.3–4, jql, 1.7.0):
(1) после approve standalone-юнита событие done называло ЧУЖОЙ todo_id
(«TODO-0074 archived» вместо закрытого; «TODO-0067 committed» при
закрытии 0068): юнит без коммита (no-diff цикл, commit пропущен —
TODO-0195) закрывается архивом, а ``_last_completed_todo`` —
commit-first: новейший awf-коммит принадлежит ПРЕДЫДУЩЕМУ юниту;
(2) одиночный ``awf start`` (background) при включённом BD-36
plan checkpoint открыл форму без предупреждения;
(3) ``awf_status``/``awf_brief`` смешивали «юнит активного рана» и
прочие inbox-юниты в одном списке «active».

Инварианты:
1. Вне рана done-событие называет юнит, закрытый ТЕКУЩИМ циклом:
   свежесть новейшего awf-коммита сравнивается со свежестью
   новейшего архива ``done/<id>/`` (mtime); коммита нет → архив,
   коммит свежее архива → коммит, равное время → архив закрываемого.
   Атрибуция ВНУТРИ рана (архив current-юнита) не меняется.
2. ``awf start`` при включённом BD-36 без ``no_checkpoints`` несёт в
   ответе поле ``checkpoint_warning`` («откроется плановый чекпоинт;
   передай no_checkpoints=true, чтобы пропустить»).
3. «Active» в статусе/брифе — только юниты очереди активного рана;
   прочие активные inbox-юниты — отдельно (``standalone_todos`` /
   строка «outside run» в карточке). Без активного рана список не
   делится (поведение прежнее).
"""
from __future__ import annotations

import os
import subprocess
import time as _time
from pathlib import Path

from awf import api, run_state
from awf.pipeline_state import clear_state, write_state

T1 = "TODO-0001"
T2 = "TODO-0002"
T3 = "TODO-0003"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Reports32")
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


def _active_todo(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


class TestDoneAttributionOutsideRun:
    """(1) Два standalone-цикла подряд, второй «без коммита» → каждый
    done-ивент называет СВОЙ юнит, а не владельца новейшего коммита."""

    def test_each_cycle_names_its_own_unit(self, tmp_git_repo):
        """Кейс из репорта: цикл 1 закрывается с коммитом, цикл 2 —
        без коммита (no-diff, commit пропущен). Красный на baseline:
        второй done-ивент называет TODO-0001 («committed and
        archived») — владельца новейшего коммита."""
        proj = _project(tmp_git_repo)

        # Цикл 1: коммит + архив своего юнита.
        _commit_awf(proj, T1)
        _archive(proj, T1)
        _clean_exit_leftover(proj)

        result1 = api.wait_for_event(proj, timeout=5, poll_interval=1)
        assert result1.event_type == "done"
        assert T1 in result1.message
        assert T2 not in result1.message

        # Цикл 2: архив БЕЗ коммита (архив строго свежее коммита).
        _time.sleep(1.1)
        _archive(proj, T2)

        result2 = api.wait_for_event(proj, timeout=5, poll_interval=1)
        assert result2.event_type == "done"
        assert T2 in result2.message
        assert T1 not in result2.message
        assert "archived" in result2.message

    def test_fresh_commit_still_wins_when_archive_older(self, tmp_git_repo):
        """Контроль: коммит НОВЕЕ архива другого юнита (цикл 2
        закоммичен, архив ещё не положен) → коммит называет свой
        юнит, как раньше (commit-first для свежеего коммита)."""
        proj = _project(tmp_git_repo)
        _commit_awf(proj, T1)
        _archive(proj, T1)
        _time.sleep(1.1)
        # Цикл 2: только коммит, архив появится позже.
        _commit_awf(proj, T2)
        _clean_exit_leftover(proj)

        result = api.wait_for_event(proj, timeout=5, poll_interval=1)

        assert result.event_type == "done"
        assert T2 in result.message
        assert T1 not in result.message


class TestStartCheckpointWarning:
    """(2) awf_start: включённый BD-36 без no_checkpoints →
    checkpoint_warning в ответе."""

    @staticmethod
    def _start_with_fake_spawn(proj: Path, monkeypatch, **kwargs):
        """start_pipeline с подменой спавна (процесс не запускается):
        фейк возвращает живой pid (процесс теста) и log-файл."""
        import awf.api.pipeline as pl

        def fake_start_in_background(
            project_dir,
            *,
            pipeline,
            from_stage,
            auto,
            timeout,
            todo_id="",
            no_checkpoints=False,
        ):
            log_file = project_dir / ".agentic" / "logs" / "awf-start.out"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("boot\n", encoding="utf-8")
            return os.getpid(), log_file, log_file.parent / "awf-start.pid"

        monkeypatch.setattr(pl, "start_in_background", fake_start_in_background)
        return pl.start_pipeline(proj, background=True, **kwargs)

    def test_warning_when_checkpoint_enabled(self, tmp_git_repo, monkeypatch):
        """Красный на baseline: поля checkpoint_warning нет в ответе."""
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "true")
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)

        result = self._start_with_fake_spawn(proj, monkeypatch)

        assert result.run_mode == "background"
        assert "BD-36" in result.checkpoint_warning
        assert "no_checkpoints=true" in result.checkpoint_warning

    def test_no_warning_with_no_checkpoints(self, tmp_git_repo, monkeypatch):
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "true")
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)

        result = self._start_with_fake_spawn(proj, monkeypatch, no_checkpoints=True)

        assert result.run_mode == "background"
        assert result.checkpoint_warning == ""

    def test_no_warning_when_checkpoint_disabled(self, tmp_git_repo, monkeypatch):
        """Конфиг/env выключили чекпоинт (AWF_PLAN_CHECKPOINT=false —
        дефолт конфеста) → предупреждения нет."""
        monkeypatch.delenv("AWF_PLAN_CHECKPOINT", raising=False)
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "false")
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)

        result = self._start_with_fake_spawn(proj, monkeypatch)

        assert result.run_mode == "background"
        assert result.checkpoint_warning == ""


class TestStatusRunSplit:
    """(3) Статус и бриф: «active» — только очередь активного рана,
    остальные inbox-юниты — отдельно."""

    @staticmethod
    def _active_run_with_queue(proj: Path, queue: list[str]) -> None:
        run_state.write_run(
            proj,
            queue=[{"todo_id": q, "pipeline": ""} for q in queue],
            index=0,
            current=queue[0],
            active=True,
        )

    def test_status_splits_run_and_outside(self, tmp_git_repo):
        """Красный на baseline: standalone-юнит лежит в active_todos,
        поля standalone_todos нет."""
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)
        _active_todo(proj, T2)
        _active_todo(proj, T3)
        self._active_run_with_queue(proj, [T1, T2])

        status = api.get_status(proj)

        assert [t["todo_id"] for t in status.active_todos] == [T2, T1]
        assert [t["todo_id"] for t in status.standalone_todos] == [T3]

    def test_brief_separates_outside_run(self, tmp_git_repo):
        """Карточка: «outside run» — отдельная строка, active-строки —
        только юниты рана."""
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)
        _active_todo(proj, T2)
        _active_todo(proj, T3)
        self._active_run_with_queue(proj, [T1, T2])

        text = api.brief(proj).text

        active_lines = [ln for ln in text.splitlines() if ln.startswith("- active:")]
        outside_lines = [ln for ln in text.splitlines() if ln.startswith("- outside run:")]
        assert T1 in text and T2 in text
        assert any(T3 in ln for ln in outside_lines), outside_lines
        assert not any(T3 in ln for ln in active_lines), active_lines

    def test_no_split_without_run(self, tmp_git_repo):
        """Без активного рана всё, как раньше: один список, standalone
        пуст."""
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)
        _active_todo(proj, T3)

        status = api.get_status(proj)

        assert [t["todo_id"] for t in status.active_todos] == [T3, T1]
        assert status.standalone_todos == []

        text = api.brief(proj).text
        assert not any(ln.startswith("- outside run:") for ln in text.splitlines())
