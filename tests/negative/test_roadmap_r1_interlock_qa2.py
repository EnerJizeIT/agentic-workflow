"""QA-находки к TODO-0218 (R1.4), iter-9 (роль agent-qa-review).

Каждый тест КРАСНЫЙ на текущей реализации и доказывает поведенческий
разрыв в границах юнита. Прод-код не трогается (доктрина 02 — правка
вперёд точечно; роль QA — только тесты).

Находка A (требование 3 юнита: «alive ИЛИ unknown запрещают разрушение»):
``_lease.live_owner`` намеренно возвращает ``None`` для НЕЧИТАЕМОЙ заявки
(«не читается ≠ мертва»), а его docstring обещает, что консервативный
preflight «считает заявку занятой отдельно (см. acquire)». Но
``_liveness.destructive_blockers`` блокирует только по живому int-pid:
пустой/битый lease-файл (владелец неизвестен) НЕ мешает reset / rollback /
force-init / remove_orphans — проект сносится, хотя владелец не доказуемо
мёртв. Парная проверка асимметрии: битый store-снимок у разрушающего
действия ОТКАЗЫВАЕТ (``check_action`` → ``StateCorrupt``) — тот же класс
«unknown», но lease его не повторяет.

Находка B (требование 4 юнита: «проверка и захват — одна критическая
секция», и это ровно окно F-H1): ``init_project(force=True)`` держит
``destructive_section``, но ``_clean_runtime`` стирает ``.agentic/state``
вместе с файлом ``.interlock``, на котором flock уже взят (reset свой
lock-файл бережёт). Пока force-init застрял между сносом и воссозданием
файла, конкурентный запуск открывает НОВЫЙ inode, берёт flock и публикует
lease — interlock не покрывает окно сноса, проект перетирается живому
запуску.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
from pathlib import Path

import pytest

from awf import api
from awf.api import _lease

TID = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings2")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _poison_lease(proj: Path) -> Path:
    """Файл заявки есть, pid нечитаем — владелец НЕ доказуемо мёртв."""
    lp = _lease.lease_path(proj)
    lp.write_text("", encoding="utf-8")
    return lp


class TestUnknownLeaseOwnerBlocksDestruction:
    def test_reset_refused_while_lease_unreadable(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        _poison_lease(proj)
        with pytest.raises(api.AwfApiError) as ei:
            api.reset_runtime(proj)
        assert "in use" in str(ei.value).lower(), str(ei.value)
        assert keep.exists(), (
            "reset снёс проект при неизвестном владельце lease (unknown ≠ free)"
        )

    def test_force_init_refused_while_lease_unreadable(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        _poison_lease(proj)
        with pytest.raises(api.AwfApiError):
            api.init_project(proj, force=True)
        assert keep.exists(), (
            "force-init снёс проект при неизвестном владельце lease"
        )

    def test_rollback_refused_while_lease_unreadable(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.create_baseline(proj, TID)
        _poison_lease(proj)
        with pytest.raises(api.AwfApiError) as ei:
            api.rollback(proj, TID, mode="hard")
        assert "in use" in str(ei.value).lower(), str(ei.value)

    def test_remove_orphans_refused_while_lease_unreadable(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        _poison_lease(proj)
        ids = api.list_orphans(proj)
        assert ids == [TID], f"ожидался орфан {TID}: {ids}"
        with pytest.raises(api.AwfApiError):
            api.remove_orphans(proj, ids)
        assert (proj / ".agentic" / "inbox" / f"{TID}.md").exists(), (
            "remove_orphans удалил задание при неизвестном владельце lease"
        )


def _child_acquire_after(project_dir_str: str, go, out) -> None:
    go.wait(timeout=30)
    try:
        lease = _lease.acquire(Path(project_dir_str))
    except Exception as exc:  # noqa: BLE001 — исход в очередь
        out.put(("refused", type(exc).__name__))
        return
    out.put(("acquired", 0))
    lease.release()


class TestForceInitInterlockCoversWipe:
    def test_launch_refused_while_force_init_holds_section(
        self, tmp_git_repo, monkeypatch
    ):
        """Пока force-init держит секцию, конкурентный запуск обязан отказать.

        ``_clean_runtime`` стирает ``.interlock`` вместе с его flock; пока
        force-init застрял после сноса, запуск открывает новый inode и берёт
        lease — окно сноса открыто (нарушение требования 4).
        """
        import awf.api.lifecycle as lc

        proj = _project(tmp_git_repo)
        ctx = mp.get_context("fork")
        go = ctx.Event()
        out = ctx.Queue()
        child = ctx.Process(
            target=_child_acquire_after, args=(str(proj), go, out)
        )
        child.start()

        cleaned = threading.Event()
        release = threading.Event()
        real_clean = lc._clean_runtime

        def _slow_clean(pd):
            result = real_clean(pd)
            cleaned.set()
            release.wait(timeout=30)
            return result

        monkeypatch.setattr(lc, "_clean_runtime", _slow_clean)

        def _run_force_init():
            try:
                api.init_project(proj, force=True)
            except Exception:  # noqa: BLE001 — исход теста не в этом
                pass

        t = threading.Thread(target=_run_force_init)
        t.start()
        try:
            assert cleaned.wait(timeout=30), "force-init не дошёл до сноса runtime"
            go.set()
            res = out.get(timeout=20)
            assert res[0] == "refused", (
                "запуск взял lease, пока force-init держал критическую секцию: "
                f"interlock не покрывает окно сноса .interlock — {res!r}"
            )
        finally:
            release.set()
            t.join(timeout=30)
            child.join(timeout=15)
