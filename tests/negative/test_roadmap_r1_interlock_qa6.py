"""QA-находка к TODO-0218 (R1.4), роль agent-qa-review (стадия 4, iter 16).

Пробел покрытия у F-ID (REVIEW-фикс 12): production-путь запуска обязан
ПУБЛИКОВАТЬ ExecutionId на проходящем пути ДО спавна ребёнка. Тесты
QA5 проверяют только обратную сторону — что отказ метку НЕ трогает.
Если публикацию из ``start_pipeline``/``continue_pipeline`` убрать (или
переставить после спавна), все существующие тесты остаются зелёными:
``read_execution_id`` даёт ``None`` → env ребёнку не передаётся →
``execution_confirmed`` трактует «метки нет = работай» (legacy), и
защита требования 2 молча исчезает. Этот файл закрывает обе стороны:

1. проходящий background-старт публикует метку ДО вызова спавна, и
   ребёнок получил бы именно её (иначе handshake мёртв);
2. отказанный ``continue`` (тот же F-ID-путь, что и ``start``, но
   отдельная дверь) не переписывает метку живого запуска.
"""
from __future__ import annotations

import os
from pathlib import Path

import awf.api.pipeline as pl
from awf import api
from awf.api import _lease

T1 = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings6")
    return tmp_git_repo


def _active_todo(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(f"# {todo_id}\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


class TestPassingStartPublishesIdentity:
    """Проходящий старт публикует ExecutionId ДО спавна ребёнка."""

    def test_background_start_publishes_marker_before_spawn(
        self, tmp_git_repo, monkeypatch
    ):
        proj = _project(tmp_git_repo)
        _active_todo(proj, T1)
        monkeypatch.setenv("AWF_PLAN_CHECKPOINT", "false")

        seen: dict = {}

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
            # Ровно то, что делает настоящий спавн: читает метку и кладёт
            # её в env ребёнка (``_background``: EXECUTION_ENV).
            seen["spawn_marker"] = _lease.read_execution_id(project_dir)
            log_file = project_dir / ".agentic" / "logs" / "awf-start.out"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("boot\n", encoding="utf-8")
            return os.getpid(), log_file, log_file.parent / "awf-start.pid"

        monkeypatch.setattr(pl, "start_in_background", fake_start_in_background)

        result = pl.start_pipeline(proj, background=True)

        assert result.run_mode == "background", result.message
        marker = _lease.read_execution_id(proj)
        assert marker, (
            "проходящий старт не опубликовал ExecutionId: ребёнок ушёл бы "
            "без метки, handshake выродился в legacy «метки нет = работай»"
        )
        assert seen["spawn_marker"] == marker, (
            "спавн вызван ДО публикации метки: ребёнок получил бы пустой "
            "или устаревший ExecutionId и провалил бы handshake"
        )
        assert _lease.execution_confirmed(proj, seen["spawn_marker"]) is True

    def test_refused_continue_keeps_live_identity(self, tmp_git_repo, monkeypatch):
        proj = _project(tmp_git_repo)
        lease = _lease.acquire(proj)
        id_a = lease.execution_id
        lease.release()

        monkeypatch.setattr(
            pl._liveness, "resolve", lambda _p: (True, os.getpid(), "state")
        )
        res = pl.continue_pipeline(proj, background=False)

        assert res.run_mode == "noop", res.message
        assert "already running" in res.message.lower(), res.message
        assert _lease.read_execution_id(proj) == id_a, (
            "отказанный continue переписал метку живого запуска — его "
            "ребёнок провалит handshake и умрёт (ноль worker'ов)"
        )
        assert _lease.execution_confirmed(proj, id_a) is True
