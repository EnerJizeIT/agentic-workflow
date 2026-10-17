"""QA-находки к TODO-0218 (R1.4): поведенческие разрывы, которых не ловят
тесты юнита `test_roadmap_r1_interlock.py`. Каждый тест КРАСНЫЙ на текущей
реализации и доказывает находку из `.agentic/outbox/BLOCKED-TODO-0218.md`.
Прод-код не трогается — только тесты (роль agent-qa-review).

Находка 1 (run/start): reset во время старта, ещё НЕ опубликовавшего lease,
проходит — нарушения требования 4 (проверка и захват — одна критическая
секция) и заголовка юнита «interlock запуска и разрушения».
Находка 2 (generation reuse): после reset_runtime поколение начинается
заново; устаревший stop_run с прежним поколением закрывает НОВЫЙ run и
приписывает ему чужую причину — тот самый дефект S3-03, что юнит закрывает.
Находка 3 (второй guard): remove_orphans (соседний разрушительный вызов)
смотрит только liveness пайплайна и игнорирует живой lease — расходится с
reset_runtime.
"""
from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

import pytest

from awf import api, run_state
from awf.api import _lease

TID = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="QaFindings")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str) -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text("# Task\n", encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _start_before_lease_publish(project_dir_str: str, entered, release) -> None:
    """Ребёнок: пройти guard acquire и замереть ПЕРЕД публикацией lease."""
    from awf.api import _lease as _l

    real = _l._publish_claim

    def _blocked(path, pid):
        entered.set()
        release.wait(timeout=30)
        return real(path, pid)

    _l._publish_claim = _blocked  # fork: патч виден только ребёнку
    try:
        _l.acquire(Path(project_dir_str))
    except Exception:  # noqa: BLE001 — итог неважен, важен эффект reset
        pass


class TestFindingStartResetWindow:
    def test_reset_refused_while_start_window_open(self, tmp_git_repo):
        """Находка 1: reset при старте в окне до публикации lease → отказ.

        Реальный второй процесс прошёл guard и стоит до `os.link`; reset
        обязан отказать (требование 4). Сейчас проходит и сносит проект.
        """
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        ctx = mp.get_context("fork")
        entered = ctx.Event()
        release = ctx.Event()
        child = ctx.Process(
            target=_start_before_lease_publish, args=(str(proj), entered, release)
        )
        child.start()
        try:
            assert entered.wait(timeout=15), "ребёнок не дошёл до окна старта"
            with pytest.raises(api.AwfApiError):
                api.reset_runtime(proj)
            assert keep.exists(), "reset снёс проект во время старта"
        finally:
            release.set()
            child.join(timeout=15)


class TestFindingGenerationReuse:
    def test_stale_stop_after_reset_leaves_new_run(self, tmp_git_repo):
        """Находка 2: поколение повторилось после reset → stale stop закрыл
        НОВЫЙ run. Ожидание: stale_operation, новый run активен."""
        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.run_start(proj, queue=[TID])
        old_state = run_state.read_run(proj)

        api.reset_runtime(proj)  # законный снос мёртвого run
        api.run_start(proj, queue=[TID])  # поколение снова 1

        from awf.api.run import stop_run

        result = stop_run(proj, old_state, "stale stop from wiped run")
        assert "stale_operation" in result.message, (
            f"устаревший stop обязан быть stale: {result.message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "устаревший stop из снесённого run закрыл новый run"
        )


class TestFindingSecondGuard:
    def test_remove_orphans_refuses_while_lease_held(self, tmp_git_repo):
        """Находка 3: живой lease запрещает reset, но не remove_orphans."""
        proj = _project(tmp_git_repo)
        _activate(proj, TID)  # без прогресса → orphan
        lease = _lease.acquire(proj)  # живой запуск (этот процесс)
        try:
            ids = api.list_orphans(proj)
            assert ids == [TID], f"ожидался орфан {TID}: {ids}"
            with pytest.raises(api.AwfApiError):
                api.remove_orphans(proj, ids)
        finally:
            lease.release()


# ─── добор QA (iter-3): непокрытые кромки юнита ─────────────────────────
# Тесты зелёные на исправленной реализации и фиксируют межлevel-поведение,
# которое правки R1.4 добавили, но ни один тест юнита не проверял:
# rollback держит общую критическую секцию, remove_orphans уважает
# Store-резервацию, устаревший finish не закрывает run, поднятый ПОСЛЕ
# reset (поколение переживает снос проекции — второй путь S3-03).


class TestRollbackInterlock:
    def test_rollback_refused_while_lease_live(self, tmp_git_repo):
        """rollback(hard) — разрушительный режим: живой launch → отказ."""
        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        lease = _lease.acquire(proj)  # живой запуск (этот процесс)
        try:
            with pytest.raises(api.AwfApiError) as ei:
                api.rollback(proj, TID, mode="hard")
            assert "in use" in str(ei.value).lower(), str(ei.value)
        finally:
            lease.release()

    def test_rollback_dry_run_ignores_lease(self, tmp_git_repo):
        """dry-run только читает — живой lease не мешает (не over-refusal)."""
        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.create_baseline(proj, TID)
        lease = _lease.acquire(proj)
        try:
            result = api.rollback(proj, TID, mode="dry-run")
            assert result.mode == "dry-run"
        finally:
            lease.release()


class TestRemoveOrphansReservation:
    def test_remove_orphans_refused_while_reservation_held(self, tmp_git_repo):
        """remove_orphans — тот же preflight, что reset: резервация → отказ."""
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, TID)  # без прогресса → orphan
        state_store.reserve(
            proj, "worker", "supervisor", operation_id="op-ro", expected_revision=0
        )
        ids = api.list_orphans(proj)
        assert ids == [TID], f"ожидался орфан {TID}: {ids}"
        with pytest.raises(api.AwfApiError):
            api.remove_orphans(proj, ids)
        assert (proj / ".agentic" / "inbox" / f"{TID}.md").exists(), (
            "remove_orphans удалил задание при живой резервации"
        )


class TestFinishGenerationAfterReset:
    def test_stale_finish_after_reset_leaves_new_run(
        self, tmp_git_repo, monkeypatch
    ):
        """Второй путь F2: run_finish, начатый ДО reset, не закрывает run,
        поднятый ПОСЛЕ reset (поколение переживает снос run.yaml)."""
        import threading

        import awf.api.run as run_mod

        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.run_start(proj, queue=[TID])
        assert run_state.generation_of(run_state.read_run(proj)) == 1

        reached = threading.Event()
        resume = threading.Event()
        real_write_report = run_mod._write_report

        def _blocking_write_report(*args, **kwargs):
            reached.set()
            resume.wait(timeout=30)
            return real_write_report(*args, **kwargs)

        monkeypatch.setattr(run_mod, "_write_report", _blocking_write_report)

        outcome: dict = {}

        def _finish():
            try:
                outcome["result"] = api.run_finish(proj, reason="stale", force=True)
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc

        t = threading.Thread(target=_finish)
        t.start()
        assert reached.wait(timeout=15), "run_finish не дошёл до записи отчёта"

        # Пока finish заблокирован — reset сносит проекцию, затем новый run.
        api.reset_runtime(proj)
        api.run_start(proj, queue=[TID])
        assert run_state.generation_of(run_state.read_run(proj)) == 2

        resume.set()
        t.join(timeout=30)
        assert not t.is_alive(), "run_finish не завершился"

        assert "exc" not in outcome, f"finish упал: {outcome.get('exc')!r}"
        assert "stale_operation" in outcome["result"].message, (
            f"устаревший finish обязан назвать stale_operation: "
            f"{outcome['result'].message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "устаревший finish закрыл run, поднятый после reset"
        )
        assert run_state.generation_of(disk) == 2


# ─── QA-находка (iter-4): сбой зеркала Store при start ──────────────────
# run_start пишет ПЕРВИЧНЫЙ run.yaml, затем зеркалит его в Store
# (publish_active_run) — БЕЗ try/except, в отличие от run_revise и
# _close_run_cas, где тот же зеркальный вызов явно защищён («a mirror
# fault must not undo the authoritative write»). Сбой Store (corrupt
# snapshot / исчерпаны ретраи StaleState) выбрасывает StateStoreError
# НАРУЖУ уже ПОСЛЕ записи run.yaml: вызывающий видит ошибку, а ран на
# диске активен — частичное состояние. Инвариант: либо успех при активном
# ране, либо отказ без активного рана.


class TestFindingStartMirrorPartialFailure:
    def test_start_mirror_fault_leaves_no_half_started_run(
        self, tmp_git_repo, monkeypatch
    ):
        import awf.api.run as run_mod
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, TID)

        def _boom(*args, **kwargs):
            raise state_store.StateStoreError("simulated mirror fault")

        monkeypatch.setattr(run_mod.state_store, "publish_active_run", _boom)

        raised = None
        try:
            api.run_start(proj, queue=[TID])
        except Exception as exc:  # noqa: BLE001
            raised = exc

        disk = run_state.read_run(proj)
        active_on_disk = bool(disk and disk.get("active"))
        assert (raised is None) == active_on_disk, (
            "run_start при сбое зеркала Store оставил частичное состояние: "
            f"raised={raised!r}, active_on_disk={active_on_disk}. Ожидалось: "
            "либо успех при активном ране, либо отказ БЕЗ активного рана."
        )


# ─── QA-находка (iter-6): сбой зеркала Store при ПРИМЕНЁННОЙ ревизии ────
# run_revise зеркалит новое поколение в Store и ГЛУШИТ сбой этого зеркала
# («mirror only — the revision is authoritative»), тогда как _close_run_cas
# считает снимок Store истиной в последней инстанции. Итог рассинхрон:
# проекция run.yaml на новом поколении, снимок active_run — на старом.
# Законный stop_run/run_finish по ТЕКУЩЕМУ состоянию тогда отвергается как
# «stale_operation» по ложной причине («run moved to a newer generation» —
# он никуда не двигался, отстало зеркало), и живой run нельзя закрыть ничем,
# кроме force-replace. Нарушен инвариант «нет вечного запрета на собственный
# stop/recovery» (доктрина 03-launch-lease, добор T3b). Проверка закрытия
# должна согласовывать проекцию и снимок, а не слепо доверять отставшему.


class TestFindingReviseMirrorDesync:
    @pytest.mark.parametrize("closer_name", ["stop_run", "run_finish"])
    def test_applied_revise_mirror_fault_leaves_run_closable(
        self, tmp_git_repo, monkeypatch, closer_name
    ):
        """Сбой зеркала при применённой ревизии → run остаётся закрываемым.

        После сбоя проекция обязана остаться согласованной со снимком Store
        (ревизия откатывается ИЛИ снимок догоняется), а закрытие ТЕКУЩЕГО
        состояния — пройти. Сейчас: проекция gen2, снимок gen1 →
        stale_operation, run активен и незакрываем (и stop, и finish).
        """
        from awf.api.run import run_finish, stop_run
        from awf.infra import state_store

        closer = {"stop_run": stop_run, "run_finish": run_finish}[closer_name]

        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        _activate(proj, "TODO-0002")
        api.run_start(proj, queue=[TID])
        assert state_store.active_run_generation(proj) == 1

        real = state_store.publish_active_run
        fail = {"on": True}

        def _maybe(project, state, _real=real, _fail=fail):
            if _fail["on"]:
                raise state_store.StateStoreError(
                    "simulated mirror fault at revise"
                )
            return _real(project, state)

        monkeypatch.setattr(state_store, "publish_active_run", _maybe)

        res = api.run_revise(
            proj, queue=["TODO-0002"], key="rev-mirror", reason="add"
        )
        assert res.action == "applied", res.message

        # Сбой зеркала проглочен — проекция и снимок разъехались.
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True
        assert run_state.generation_of(disk) == 2

        # Законное закрытие ТЕКУЩЕГО run обязано пройти: проекция и её
        # поколение — те же, что вернул run_state.read_run.
        fail["on"] = False
        if closer_name == "stop_run":
            result = closer(proj, disk, "legit stop after revise")
            assert result.action == "stopped", (
                "run, чья проекция совпала по поколению, обязан "
                f"закрываться, а не получать отказ: {result.message!r}"
            )
        else:
            result = closer(proj, reason="legit finish after revise")
            assert result.active is False, (
                "run_finish текущего поколения обязан закрыть run, а не "
                f"остаться active: {result.message!r}"
            )
        closed = run_state.read_run(proj)
        assert closed is not None and closed.get("active") is False, (
            "активный run не закрылся после проглатывания сбоя зеркала"
        )

# ─── QA-находка (iter-6): ложный текст отказа force-start при сбое зеркала ─
# _rollback_projection восстанавливает ПРЕДЫДУЩУЮ проекцию — при force-replace
# это СТАРЫЙ, всё ещё активный run. Но текст отказа безусловно говорит «no
# active run remains», хотя активный run на диске остался. Вызывающий
# (супервизор) получает факт, противоречащий состоянию: решит, что ранов нет.
# Инвариант: текст отказа обязан совпадать с фактом на диске.


class TestFindingForceStartFaultMessage:
    def test_force_start_mirror_fault_message_matches_disk(
        self, tmp_git_repo, monkeypatch
    ):
        """Сбой зеркала при force-replace → текст не отрицает живого run."""
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, TID)
        api.run_start(proj, queue=[TID])
        assert (run_state.read_run(proj) or {}).get("active") is True

        real = state_store.publish_active_run

        def _boom(project, state, _real=real):
            raise state_store.StateStoreError("simulated mirror fault")

        monkeypatch.setattr(state_store, "publish_active_run", _boom)

        raised = None
        try:
            api.run_start(proj, queue=[TID], force=True)
        except Exception as exc:  # noqa: BLE001
            raised = exc
        assert raised is not None, "force-start при сбое зеркала обязан отказать"

        disk = run_state.read_run(proj)
        active_on_disk = bool(disk and disk.get("active"))
        assert active_on_disk, (
            "предыдущий run обязан быть сохранён (откат к прежней проекции)"
        )
        assert "no active run remains" not in str(raised), (
            "текст отказа противоречит диску: активный run остался, а сообщение "
            f"говорит, что его нет — {str(raised)!r}"
        )
