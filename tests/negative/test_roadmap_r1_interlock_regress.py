"""QA-регресс (iter-8) к TODO-0218 (R1.4): кромки interlock/CAS, которые
ни один тест юнита не покрывает. Тесты ЗЕЛЁНЫЕ на текущей реализации —
фиксируют уже работающие поведения, чтобы будущая правка не сломала их
молча:

* ``_close_run_cas`` watermark: поколение ВЫШЕ durable-счётчика Store
  невозможно (Store его не выдавал) → закрытие отвергается как stale;
* reentrancy критической секции: ``reset(orphans=True)`` держит interlock
  и внутри зовёт публичный ``remove_orphans`` — повторный flock не должен
  заблокировать сам себя;
* ``reset_runtime`` отказывает (не ждёт), когда секцию держит ДРУГОЙ
  процесс, и не удаляет данные под чужой операцией.
"""
from __future__ import annotations

import multiprocessing as mp
import os
from pathlib import Path

import pytest

from awf import api, run_state
from awf.api import _lease, _liveness

TID = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="InterlockRegress")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _child_hold_interlock(project_dir_str: str, ready, release) -> None:
    """Ребёнок держит interlock-секцию, пока родитель пробует reset."""
    from awf.api._interlock import destructive_section

    with destructive_section(project_dir_str, label="test-hold"):
        ready.set()
        release.wait(timeout=30)


class TestInterlockRegressionEdges:
    def test_close_generation_above_watermark_is_refused(self, tmp_git_repo):
        """Поколение ВЫШЕ durable-счётчика не могло быть выдано → stale.

        Предохранитель watermark в ``_close_run_cas``: объект с номером,
        которого Store не выделял, не закрывает живой run.
        """
        from awf.api.run import stop_run

        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.run_start(proj, queue=[TID])

        result = stop_run(proj, {"generation": 5, "active": True}, "bogus future")
        assert "stale_operation" in result.message, result.message
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "объект с выдуманным (сверх watermark) поколением закрыл живой run"
        )
        assert run_state.generation_of(disk) == 1

    def test_reset_orphans_mode_reenters_interlock(self, tmp_git_repo):
        """reset(orphans=True) внутри уже взятой секции не блокирует сам себя.

        reset держит interlock и внутри вызывает публичный remove_orphans,
        который берёт ту же секцию — reentrancy обязана пропустить повторный
        flock, иначе взаимоблокировка или ложный отказ на живом пути.
        """
        proj = _project(tmp_git_repo)
        _activate(proj, TID)  # без прогресса → orphan
        result = api.reset_runtime(proj, orphans=True)
        assert result.mode == "orphans", result.message
        assert result.orphan_ids == [TID]

    def test_reset_refused_while_another_process_holds_interlock(
        self, tmp_git_repo
    ):
        """Секцию держит ДРУГОЙ процесс → reset отказывает, а не ждёт."""
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        release = ctx.Event()
        child = ctx.Process(
            target=_child_hold_interlock, args=(str(proj), ready, release)
        )
        child.start()
        try:
            assert ready.wait(timeout=15), "ребёнок не взял interlock"
            with pytest.raises(api.AwfApiError):
                api.reset_runtime(proj)
            assert keep.exists(), (
                "reset снёс проект под чужой критической секцией"
            )
        finally:
            release.set()
            child.join(timeout=15)


# ─── QA final pass (R1.4): the two newest mechanisms, pinned directly ───
# The integration tests prove the OUTCOMES (a destruction is refused while the
# owner is unknown; the critical-section file covers the wipe). These pin the
# RAW mechanisms so a later edit cannot quietly take them back: the claim
# classifier (missing / unreadable / dead / alive) and the survival of the
# critical-section file across a destructive wipe. No unit id appears here —
# the ratchet counts every line carrying the marker word.


class TestClaimClassification:
    def test_missing_claim_is_free(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        if lp.exists():
            lp.unlink()
        assert _lease.claim_status(proj) == (_lease.CLAIM_MISSING, None)

    def test_unreadable_claims_are_unknown(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        for raw in ("", "   \n", "not-a-pid", "12.5"):
            lp.write_text(raw, encoding="utf-8")
            status, owner = _lease.claim_status(proj)
            assert status == _lease.CLAIM_UNKNOWN, (raw, status)
            assert owner is None

    def test_symlinked_claim_is_unknown(self, tmp_git_repo, tmp_path):
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        if lp.exists():
            lp.unlink()
        target = tmp_path / "foreign.lease"
        target.write_text(str(os.getpid()), encoding="utf-8")
        lp.symlink_to(target)
        assert _lease.claim_status(proj) == (_lease.CLAIM_UNKNOWN, None), (
            "a symlinked claim must not be dereferenced: the real owner is "
            "unknown, not proven dead"
        )

    def test_readable_dead_pid_is_dead(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        lp.write_text("2147483648\n", encoding="utf-8")
        assert _lease.claim_status(proj) == (_lease.CLAIM_DEAD, 2147483648)

    def test_readable_live_pid_is_alive(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        lp.write_text(f"{os.getpid()}\n", encoding="utf-8")
        assert _lease.claim_status(proj) == (_lease.CLAIM_ALIVE, os.getpid())


class TestDestructiveBlockersClassify:
    def test_unknown_claim_blocks(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        _lease.lease_path(proj).write_text("", encoding="utf-8")
        blockers = _liveness.destructive_blockers(proj)
        assert blockers, "an unreadable owner must be a blocker, not free"

    def test_dead_and_missing_claims_do_not_block(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        assert _liveness.destructive_blockers(proj) == [], (
            "a free project must not be refused"
        )
        _lease.lease_path(proj).write_text("2147483648\n", encoding="utf-8")
        assert _liveness.destructive_blockers(proj) == [], (
            "a readable dead owner must reconcile, not refuse"
        )


class TestCriticalSectionFileSurvives:
    def test_reset_preserves_interlock_inode(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lock = proj / ".agentic" / "state" / ".interlock"
        assert lock.exists(), "init must seed the critical-section file"
        before = lock.stat().st_ino
        api.reset_runtime(proj)
        assert lock.exists(), "reset must not remove its own critical section"
        assert lock.stat().st_ino == before, (
            "reset replaced the critical-section inode — a concurrent launch "
            "would flock a fresh file and slip through the wipe window"
        )

    def test_force_init_preserves_interlock_inode(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lock = proj / ".agentic" / "state" / ".interlock"
        before = lock.stat().st_ino
        api.init_project(proj, force=True)
        assert lock.exists(), "force-init must keep its own critical section"
        assert lock.stat().st_ino == before, (
            "force-init replaced the critical-section inode — the wipe window "
            "loses its coverage"
        )
