"""R1.4: interlock запуска и разрушения (roadmap R1).

Три доказанных дефекта аудита:

1. S1-02 — ``_lease.acquire`` создавал ПУСТОЙ файл (O_EXCL), pid писался
   вторым шагом; конкурент читал пустоту, считал владельца мёртвым и
   удалял lease. Два запуска получали один проект.
2. S9-02 — ``reset_runtime`` не проверял живой владелец: активный запуск
   или lease затирались. Нужен общий interlock с запуском.
3. S3-03 — ``run_finish``/``stop_run`` закрывали АКТУАЛЬНЫЙ run без
   проверки поколения: устаревший finish закрывал новый run после
   ``run_start(force=True)`` и приписывал ему чужую причину/отчёт.

Плюс добор приёмки 0215: захват lease не следует симлинку ``.agentic``
наружу проекта.

Часть проверок — на РЕАЛЬНЫХ двух локальных процессах (multiprocessing,
без моков lock) на синтетическом проекте.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from awf import api, run_state
from awf.api import _lease, _liveness

# ─── helpers ────────────────────────────────────────────────────────────


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="Interlock")
    return tmp_git_repo


def _activate(proj: Path, todo_id: str, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")
    (inbox / f"{todo_id}.ready").write_text("", encoding="utf-8")


def _dead_pid() -> int:
    pid = 2**31
    assert not _liveness.probe_alive(pid)
    return pid


def _child_acquire(project_dir_str: str, barrier, queue) -> None:
    """Дочерний процесс: встать на барьер и попытаться взять lease."""
    from awf.api import _lease as _l

    try:
        barrier.wait(timeout=30)
    except Exception:  # noqa: BLE001 — surfaced in the parent
        pass
    try:
        lease = _l.acquire(Path(project_dir_str))
    except Exception as exc:  # noqa: BLE001
        queue.put(("err", type(exc).__name__, str(exc)))
        return
    queue.put(("ok", os.getpid(), ""))
    time.sleep(1.5)  # держим lease, пока второй процесс пытается войти
    lease.release()


def _child_hold_lease(project_dir_str: str, ready, release) -> None:
    from awf.api import _lease as _l

    lease = _l.acquire(Path(project_dir_str))
    ready.set()
    release.wait(timeout=30)
    lease.release()


# ─── S1-02: атомарный lease, нечитаемый владелец не мёртв ───────────────


class TestLeaseAtomicity:
    def test_empty_lease_is_not_treated_as_dead(self, tmp_git_repo):
        """Нечитаемая заявка (файл есть, pid не разбирается) — не мертва.

        Старый код: owner=None → unlink → acquire проходит (два владельца).
        Новый: заявка не удаляется, захват отклоняется.
        """
        proj = _project(tmp_git_repo)
        lp = _lease.lease_path(proj)
        lp.write_text("", encoding="utf-8")  # пустая заявка (S1-02)

        with pytest.raises(Exception) as ei:
            _lease.acquire(proj)
        assert "contended" in str(ei.value).lower() or "unreadable" in str(
            ei.value
        ).lower(), f"ожидался отказ на нечитаемой заявке, получили: {ei.value!r}"
        assert lp.exists(), "нечитаемая заявка не должна удаляться как мёртвая"
        assert lp.read_text(encoding="utf-8") == "", "заявку нельзя перезаписать"

    def test_two_processes_one_lease(self, tmp_git_repo):
        """Матрица R1, строка 1: два процесса start → ровно один владелец."""
        proj = _project(tmp_git_repo)
        ctx = mp.get_context("fork")
        barrier = ctx.Barrier(2)
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=_child_acquire, args=(str(proj), barrier, queue))
            for _ in range(2)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0, "дочерний процесс не завершился"

        results = [queue.get(timeout=5) for _ in range(2)]
        ok = [r for r in results if r[0] == "ok"]
        err = [r for r in results if r[0] == "err"]
        assert len(ok) == 1, f"два процесса взяли lease одного проекта: {results}"
        assert len(err) == 1 and err[0][1] == "LeaseHeldError", (
            f"проигравший должен получить LeaseHeldError: {results}"
        )

    def test_symlinked_agentic_refused(self, tmp_git_repo, tmp_path):
        """Добор 0215: симлинк ``.agentic`` наружу → отказ, не утечка lease."""
        proj = _project(tmp_git_repo)
        outside = tmp_path / "outside"
        outside.mkdir()
        agentic = proj / ".agentic"
        # Переносим реальный .agentic и ставим на его место симлинк наружу.
        import shutil

        shutil.move(str(agentic), str(outside / "agentic"))
        os.symlink(str(outside / "agentic"), str(agentic))

        with pytest.raises(api.AwfApiError):
            _lease.acquire(proj)
        assert not (outside / "agentic" / "logs" / _lease.LEASE_NAME).exists(), (
            "lease не должен создаваться по симлинку наружу проекта"
        )


# ─── S9-02: reset не разрушает живое ────────────────────────────────────


class TestResetInterlock:
    def test_reset_refused_while_lease_live(self, tmp_git_repo):
        """reset при живом lease (запуск в процессе) → отказ до удаления."""
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        lease = _lease.acquire(proj)  # владелец — этот (живой) процесс
        try:
            with pytest.raises(api.AwfApiError) as ei:
                api.reset_runtime(proj)
            assert "in use" in str(ei.value).lower()
            assert keep.exists(), "reset удалил задание живого владельца"
        finally:
            lease.release()

    def test_reset_refused_while_child_process_holds_lease(self, tmp_git_repo):
        """Матрица R1, строка 2: reset во время start → отказ, живые данные целы.

        Реальный дочерний процесс держит lease; reset обязан отказать.
        """
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        ctx = mp.get_context("fork")
        ready = ctx.Event()
        release = ctx.Event()
        child = ctx.Process(
            target=_child_hold_lease, args=(str(proj), ready, release)
        )
        child.start()
        try:
            assert ready.wait(timeout=15), "дочерний процесс не взял lease"
            with pytest.raises(api.AwfApiError):
                api.reset_runtime(proj)
            assert keep.exists(), "reset снёс данные во время запуска"
        finally:
            release.set()
            child.join(timeout=15)

    def test_reset_refused_while_reservation_held(self, tmp_git_repo):
        """Store-резервация (долгое действие в полёте) запрещает разрушение."""
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        state_store.reserve(
            proj, "worker", "supervisor", operation_id="op-r1", expected_revision=0
        )
        with pytest.raises(api.AwfApiError) as ei:
            api.reset_runtime(proj)
        assert "state store" in str(ei.value).lower()
        assert keep.exists()

    def test_reset_proceeds_when_owner_is_stale(self, tmp_git_repo):
        """Мёртвый владелец не блокирует: reconcile проходит (нет over-refusal)."""
        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])
        # lease с детерминированно мёртвым pid.
        _lease.lease_path(proj).write_text(f"{_dead_pid()}\n", encoding="utf-8")
        result = api.reset_runtime(proj)
        assert result.mode != "noop"
        assert run_state.read_run(proj) is None, "ghost run не вычищен"


# ─── S3-03: CAS-закрытие run ────────────────────────────────────────────


class TestRunCloseCas:
    def test_stale_stop_run_does_not_close_new_generation(self, tmp_git_repo):
        """Устаревший stop_run после run_start(force=True) → отказ, run жив."""
        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])
        old_state = run_state.read_run(proj)
        assert run_state.generation_of(old_state) == 1

        api.run_start(proj, queue=["TODO-0001"], force=True)  # gen → 2
        assert run_state.generation_of(run_state.read_run(proj)) == 2

        from awf.api.run import stop_run

        result = stop_run(proj, old_state, "late stale stop")
        assert "stale_operation" in result.message, (
            f"устаревший stop обязан назвать stale_operation: {result.message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "устаревший stop закрыл новый run"
        )
        assert run_state.generation_of(disk) == 2, "поколение нового run изменено"

    def test_stale_run_finish_does_not_close_new_generation(
        self, tmp_git_repo, monkeypatch
    ):
        """Гонка finish: между чтением и записью вклинился новый run.

        run_finish обязан закрыть только то поколение, что прочитал;
        поколение ушло вперёд → stale_operation, новый snapshot цел.
        """
        import awf.api.run as run_mod

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        api.run_start(proj, queue=["TODO-0001"])
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
                outcome["result"] = api.run_finish(proj, reason="stale")
            except Exception as exc:  # noqa: BLE001
                outcome["exc"] = exc

        t = threading.Thread(target=_finish)
        t.start()
        assert reached.wait(timeout=15), "run_finish не дошёл до записи отчёта"

        # Пока finish заблокирован — заменяем run новым поколением.
        api.run_start(proj, queue=["TODO-0001"], force=True)
        resume.set()
        t.join(timeout=30)
        assert not t.is_alive(), "run_finish не завершился"

        assert "exc" not in outcome, f"finish упал: {outcome.get('exc')!r}"
        assert "stale_operation" in outcome["result"].message, (
            f"устаревший finish обязан назвать stale_operation: "
            f"{outcome['result'].message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk.get("active") is True, "устаревший finish закрыл новый run"
        assert run_state.generation_of(disk) == 2


# ─── F2-root (аудитор-репро): поколение переживает reset через revise ───
#
# Тесты QA (TestFindingGenerationReuse / TestFinishGenerationAfterReset)
# идут путём run_start → reset → run_start — БЕЗ run_revise. Там Store и
# проекция равны, поэтому дыра не открывается. Аудитор нашёл корень глубже:
# run_revise двигал поколение ПРОЕКЦИИ, не выделяя из durable-счётчика Store.
# После reset проекция снесена → новый run снова брал то же число → старый
# объект CAS-совпадал с новым. Фикс: единственный источник — Store-счётчик.


def _revise(proj: Path, extra: str = "TODO-0002") -> None:
    """Добавить элемент в очередь — ревизия поднимает ЦИКЛ (поколение)."""
    res = api.run_revise(proj, queue=[extra], key=f"rev-{extra}", reason="add")
    assert res.action == "applied", f"ревизия не применилась: {res.message}"


class TestGenerationSingleSource:
    def test_stale_stop_after_revise_and_reset_leaves_new_run(self, tmp_git_repo):
        """Аудитор-репро строки 3: run_start → run_revise → reset → run_start
        → stop_run(старый) обязан отказать; новый run жив."""
        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")
        api.run_start(proj, queue=["TODO-0001"])
        _revise(proj)
        old_state = run_state.read_run(proj)  # снят ПОСЛЕ ревизии
        assert run_state.generation_of(old_state) == 2

        api.reset_runtime(proj)
        api.run_start(proj, queue=["TODO-0001"])
        assert run_state.generation_of(run_state.read_run(proj)) == 3

        from awf.api.run import stop_run

        result = stop_run(proj, old_state, "stale stop from pre-reset revised run")
        assert "stale_operation" in result.message, (
            f"устаревший stop обязан назвать stale_operation: {result.message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "устаревший stop из pre-reset revised run закрыл новый run"
        )
        assert run_state.generation_of(disk) == 3, "поколение нового run изменено"

    def test_stale_finish_after_revise_and_reset_leaves_new_run(
        self, tmp_git_repo, monkeypatch
    ):
        """Тот же путь через run_finish: устаревший finish не закрывает run,
        поднятый после reset, и не снимает его stage-state."""
        import awf.api.run as run_mod

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")
        api.run_start(proj, queue=["TODO-0001"])
        _revise(proj)
        assert run_state.generation_of(run_state.read_run(proj)) == 2

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

        api.reset_runtime(proj)
        api.run_start(proj, queue=["TODO-0001"])
        assert run_state.generation_of(run_state.read_run(proj)) == 3

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
        assert run_state.generation_of(disk) == 3

    def test_generation_allocated_only_from_store(self, tmp_git_repo):
        """Инвариант: каждое поколение — выделенное значение Store-счётчика.

        После start / revise / reset+start durable-счётчик и поколение
        активного run совпадают, значения строго растут, reset счётчик не
        обнуляет. Так «allocations только через Store» проверяется
        поведенчески: независимого инкремента проекции, который рассинхронит
        их, тест бы поймал.
        """
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")

        api.run_start(proj, queue=["TODO-0001"])
        gen_start = run_state.generation_of(run_state.read_run(proj))
        assert gen_start == state_store.current_run_generation(proj) == 1

        _revise(proj)
        gen_rev = run_state.generation_of(run_state.read_run(proj))
        assert gen_rev == state_store.current_run_generation(proj) == 2

        api.reset_runtime(proj)
        # reset снёс проекцию, но durable-счётчик жив (память поколений).
        assert state_store.current_run_generation(proj) == gen_rev

        api.run_start(proj, queue=["TODO-0001"])
        gen_new = run_state.generation_of(run_state.read_run(proj))
        assert gen_new == state_store.current_run_generation(proj)
        assert gen_new > gen_rev > gen_start, (
            "поколение обязано строго расти через reset"
        )

    def test_idempotent_revision_replay_does_not_block_close(self, tmp_git_repo):
        """Дыра в счётчике (повтор ревизии тем же ключом) не мешает закрыть run.

        Повтор ревизии с уже применённым ключом ничего не пишет, но
        Store-поколение выделяется (счётчик уходит вперёд активного run).
        Это не staleness: stop_run обязан закрыть ТЕКУЩИЙ run, а не отказать
        из-за «счётчик впереди». Проверка закрытия сверяется со снимком
        активного run, а счётчик — лишь watermark.
        """
        from awf.api.run import stop_run
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")
        _activate(proj, "TODO-0002")
        _activate(proj, "TODO-0003")
        api.run_start(proj, queue=["TODO-0001"])
        _revise(proj, "TODO-0002")
        assert run_state.generation_of(run_state.read_run(proj)) == 2

        # Тот же ключ с ТЕМ ЖЕ запросом (AA1: ключ называет одно
        # содержимое): no-op, однако Store-поколение уже выделено (gap в
        # счётчике).
        replay = api.run_revise(
            proj, queue=["TODO-0002"], key="rev-TODO-0002", reason="add"
        )
        assert replay.action == "noop", replay.message
        assert state_store.current_run_generation(proj) == 3, (
            "no-op повтор всё равно выделяет поколение"
        )
        assert run_state.generation_of(run_state.read_run(proj)) == 2, (
            "активный run не должен двигаться на no-op повторе"
        )

        result = stop_run(proj, run_state.read_run(proj), "regular stop")
        assert result.action == "stopped", (
            f"текущий run обязан закрыться, а не получить отказ: {result.message}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is False


# ─── QA-3 (REVIEW-фикс 3): сбой зеркала Store при run_start ─────────────
# run_start пишет первичный run.yaml, затем зеркалит его в Store. Сбой
# зеркала раньше вылетал наружу ПОСЛЕ записи run.yaml — частичный старт:
# вызывающий видел ошибку, а ран на диске активен. Инвариант: либо успех
# при активном ране, либо отказ БЕЗ активного рана.


class TestStartMirrorFaultRollback:
    def test_start_mirror_fault_rolls_back_and_refuses(
        self, tmp_git_repo, monkeypatch
    ):
        """Сбой зеркала → чистый отказ, run.yaml откатан, повтор проходит."""
        import awf.api.run as run_mod
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")

        real_publish = run_mod.state_store.publish_active_run
        fail = {"on": True}

        def _maybe(project, state):
            if fail["on"]:
                raise state_store.StateStoreError("simulated mirror fault")
            return real_publish(project, state)

        monkeypatch.setattr(run_mod.state_store, "publish_active_run", _maybe)

        with pytest.raises(api.AwfApiError) as ei:
            api.run_start(proj, queue=["TODO-0001"])
        assert "Store mirror failed" in str(ei.value), str(ei.value)
        assert run_state.read_run(proj) is None, (
            "отказ старта оставил активную проекцию run.yaml (частичный старт)"
        )

        fail["on"] = False
        result = api.run_start(proj, queue=["TODO-0001"])
        assert result.active is True
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True

    def test_start_mirror_and_rollback_fault_names_partial_state(
        self, tmp_git_repo, monkeypatch
    ):
        """Сбой зеркала И отката → громкий текст, состояние названо явно."""
        import awf.api.run as run_mod
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, "TODO-0001")

        def _boom(*args, **kwargs):
            raise state_store.StateStoreError("simulated mirror fault")

        monkeypatch.setattr(run_mod.state_store, "publish_active_run", _boom)
        monkeypatch.setattr(
            run_mod, "_rollback_projection", lambda path, prev: "disk full"
        )

        with pytest.raises(api.AwfApiError) as ei:
            api.run_start(proj, queue=["TODO-0001"])
        msg = str(ei.value)
        assert "rollback failed too" in msg, msg
        assert "may still be active" in msg, msg


# ─── REVIEW-фикс 5 (аудитор iter-8): force-init — третья дверь S3-03 ────
# init_project(force=True) — разрушительный путь: до этого он не брал
# interlock и не звал общий destructive-preflight (гонка: force-init
# проходит, пока старт публикует заявку — проект сносится живому запуску),
# а _clean_runtime сносил state/control.json → durable-поколение
# обнулялось; устаревший stop_run(old) после force-init закрывал НОВЫЙ run.

TID_FORCE = "TODO-0001"


def _child_start_before_publish(project_dir_str: str, entered, release) -> None:
    """Ребёнок: занять окно старта и замереть, пока родитель пробует force-init.

    На исправленном коде — замереть ПЕРЕД публикацией заявки (interlock уже
    взят, это и есть коллизия). На baseline атомарной публикации нет, поэтому
    держим обычный lease: старая force-init его не проверяет — тест красный
    реальным утверждением, а не отсутствующим символом.
    """
    from awf.api import _lease as _l

    real = getattr(_l, "_publish_claim", None)
    if real is None:  # baseline: только старый lease
        try:
            lease = _l.acquire(Path(project_dir_str))
        except Exception:  # noqa: BLE001
            lease = None
        entered.set()
        release.wait(timeout=30)
        if lease is not None:
            lease.release()
        return

    def _blocked(path, pid):
        entered.set()
        release.wait(timeout=30)
        return real(path, pid)

    _l._publish_claim = _blocked
    try:
        _l.acquire(Path(project_dir_str))
    except Exception:  # noqa: BLE001 — итог неважен, важен эффект force-init
        pass


class TestForceInitInterlock:
    def test_force_init_preserves_generation_for_stale_close(self, tmp_git_repo):
        """(а) аудитор-репро: gen1 → force-init → новый run; устаревший
        stop_run(gen1) обязан отказать, новый run жив."""
        proj = _project(tmp_git_repo)
        _activate(proj, TID_FORCE)
        api.run_start(proj, queue=[TID_FORCE])
        old_state = run_state.read_run(proj)
        assert run_state.generation_of(old_state) == 1

        api.init_project(proj, force=True)  # разрушительный re-init
        api.run_start(proj, queue=[TID_FORCE])
        new_gen = run_state.generation_of(run_state.read_run(proj))
        assert new_gen > 1, (
            "force-init обнулил durable-поколение run (S3-03 через force-init)"
        )

        from awf.api.run import stop_run

        result = stop_run(proj, old_state, "stale stop from pre-force-init run")
        assert "stale_operation" in result.message, (
            f"устаревший stop обязан назвать stale_operation: {result.message!r}"
        )
        disk = run_state.read_run(proj)
        assert disk is not None and disk.get("active") is True, (
            "устаревший stop из pre-force-init run закрыл НОВЫЙ run"
        )
        assert run_state.generation_of(disk) == new_gen

    def test_force_init_refused_while_start_window_open(self, tmp_git_repo):
        """(б) гонка: force-init при старте в окне до публикации заявки →
        отказ, проект цел (общая критическая секция)."""
        proj = _project(tmp_git_repo)
        keep = proj / ".agentic" / "inbox" / "keep.txt"
        keep.write_text("precious", encoding="utf-8")
        ctx = mp.get_context("fork")
        entered = ctx.Event()
        release = ctx.Event()
        child = ctx.Process(
            target=_child_start_before_publish, args=(str(proj), entered, release)
        )
        child.start()
        try:
            assert entered.wait(timeout=15), "ребёнок не дошёл до окна старта"
            with pytest.raises(api.AwfApiError):
                api.init_project(proj, force=True)
            assert keep.exists(), "force-init снёс проект во время старта"
        finally:
            release.set()
            child.join(timeout=15)


class TestEnsureInterlockSafePath:
    def test_reinit_refuses_symlinked_state_dir(self, tmp_git_repo, tmp_path):
        """F-T1: симлинк ``state`` наружу → re-init отказывает, файл секции
        не создаётся за пределами проекта (bare touch уводил его по ссылке)."""
        import shutil

        proj = _project(tmp_git_repo)
        state = proj / ".agentic" / "state"
        outside = tmp_path / "outside_state"
        outside.mkdir()
        state.mkdir(parents=True, exist_ok=True)  # baseline: init may omit it
        shutil.rmtree(state)
        os.symlink(str(outside), str(state))

        with pytest.raises(api.AwfApiError):
            api.init_project(proj)
        assert not (outside / ".interlock").exists(), (
            "файл критической секции создан по симлинку state наружу проекта"
        )


class TestPosixRefusalWrapped:
    def test_lease_guard_posix_refusal_is_api_error(self, tmp_git_repo, monkeypatch):
        """F-T2: ``require_posix`` в пути захвата lease вне обёртки → сырой
        ManagedPathError. Ожидается AwfApiError (единый тип отказа).

        Идём через публичный ``_lease.acquire`` — на baseline он ещё не
        обёрнут, на исправленном коде ``require_posix`` внутри единой
        обёртки (launch_section/guard)."""
        from awf.api import _lease as _lease_mod
        from awf.infra import managed_fs

        proj = _project(tmp_git_repo)

        def _boom(op):
            raise managed_fs.ManagedPathError("non-posix platform")

        monkeypatch.setattr(managed_fs, "require_posix", _boom)
        with pytest.raises(api.AwfApiError):
            _lease_mod.acquire(proj)

    def test_interlock_posix_refusal_is_api_error(self, tmp_git_repo, monkeypatch):
        """F-T2: win32-ветка interlock (``launch_section``) зовёт
        ``require_posix`` вне обёртки → сырой ManagedPathError. Ожидается
        AwfApiError.

        Идём через публичный ``reset_runtime`` (берёт interlock-секцию), а
        не через импорт ``_interlock``: на baseline модуля ещё нет, а тест
        обязан падать реальным утверждением, а не отсутствующим символом."""
        import sys as _sys

        from awf.infra import managed_fs

        proj = _project(tmp_git_repo)

        def _boom(op):
            raise managed_fs.ManagedPathError("non-posix platform")

        monkeypatch.setattr(managed_fs, "require_posix", _boom)
        monkeypatch.setattr(_sys, "platform", "win32")
        with pytest.raises(api.AwfApiError):
            api.reset_runtime(proj)


# ─── REVIEW-фикс 7: F-R1, F-R2, F-P1, F-P2, F-P3 ────────────────────────


class TestExecutionIdHandshake:
    """F-R1 (требование 2): ребёнок несёт ExecutionId и сверяет его с
    текущей заявкой запуска; устаревший/чужой id работу не выполняет."""

    def test_execution_confirmed_rejects_foreign_and_missing_id(self, tmp_git_repo):
        """Прямая семантика: свой id подтверждается, чужой и пустой — нет."""
        proj = _project(tmp_git_repo)
        lease = _lease.acquire(proj)
        try:
            assert (
                _lease.execution_confirmed(proj, lease.execution_id) is True
            ), "собственный ExecutionId обязан подтверждаться"
            assert (
                _lease.execution_confirmed(proj, lease.execution_id + "-other")
                is False
            ), "чужой ExecutionId не подтверждается"
            assert (
                _lease.execution_confirmed(proj, "") is False
            ), "при живой метке пустой ExecutionId не подтверждается"
        finally:
            lease.release()

    def test_child_with_stale_execution_id_performs_no_work(
        self, tmp_git_repo, monkeypatch
    ):
        """Ребёнок старого запуска (метку уже переписал новый запуск) тихо
        выходит: run_pipeline не вызывается, работа не выполняется."""
        import awf.api.pipeline as pl
        import awf.orchestrator as orch

        proj = _project(tmp_git_repo)
        # Живая заявка с ЧУЖИМ id — как будто метку переписал новый запуск.
        lease = _lease.acquire(proj)
        ran = {"called": False}

        def _must_not_run(*a, **k):  # noqa: ANN002, ANN003
            ran["called"] = True
            raise AssertionError("run_pipeline вызван для устаревшего ребёнка")

        monkeypatch.setattr(orch, "run_pipeline", _must_not_run, raising=False)
        # В тестовом процессе argv не ``-m awf start`` — эмулируем ребёнка.
        monkeypatch.setenv("AWF_BACKGROUND_CHILD", "1")
        monkeypatch.setenv("AWF_EXECUTION_ID", "stale-orphan-id")
        monkeypatch.setattr(pl, "_is_background_child", lambda: True)
        try:
            result = pl.start_pipeline(proj, background=False)
        finally:
            lease.release()

        assert result.run_mode == "noop", result.message
        assert "ExecutionId" in result.message
        assert ran["called"] is False


class TestActiveRunGenerationHonest:
    """F-P2: ``active_run_generation`` не выдаёт поколение ЗАКРЫТОГО run."""

    def test_closed_run_mirror_is_not_active(self, tmp_git_repo):
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        state_store.publish_active_run(proj, {"generation": 7, "active": False})
        assert state_store.active_run_generation(proj) is None, (
            "зеркало закрытого run не должно выдаваться как активное поколение"
        )

    def test_active_run_mirror_is_active(self, tmp_git_repo):
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        state_store.publish_active_run(proj, {"generation": 3, "active": True})
        assert state_store.active_run_generation(proj) == 3


class TestUnknownClaimText:
    """F-P3: отказ по нечитаемой заявке называет файл заявки."""

    def test_unknown_claim_refusal_names_lease_file(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        # пустой файл заявки → CLAIM_UNKNOWN (владелец нечитаем)
        _lease.lease_path(proj).write_text("", encoding="utf-8")
        blockers = _liveness.destructive_blockers(proj)
        text = " ".join(blockers)
        assert _lease.CLAIM_UNKNOWN is not None
        assert "awf-launch.lease" in text, (
            "ветка UNKNOWN обязана назвать файл заявки (awf_kill не поможет)"
        )


class TestInitNonPosix:
    """F-P1: ``awf init`` — безопасная операция, на не-POSIX не падает."""

    def test_init_succeeds_when_not_posix(self, tmp_git_repo, monkeypatch):
        from awf.infra import managed_fs

        monkeypatch.setattr(managed_fs, "is_posix", lambda: False)
        result = api.init_project(tmp_git_repo, project_name="NonPosix")
        assert result.project_name == "NonPosix"
        assert not (
            tmp_git_repo / ".agentic" / "state" / ".interlock"
        ).exists(), "файл секции (flock) не создаётся на не-POSIX"


class TestDestructivePreview:
    """F-R2 (требование 6): preview разрушения со scope/revision/digest;
    исполнение требует ТОТ ЖЕ план, stale-план отвергается."""

    def test_preview_without_plan_is_refused(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        with pytest.raises(api.AwfApiError):
            api.execute_destruction(proj, None, confirm="")

    def test_execute_with_wrong_confirm_is_refused(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        plan = api.preview_destruction(proj, "reset")
        assert plan.digest and plan.scope
        with pytest.raises(api.AwfApiError):
            api.execute_destruction(proj, plan, confirm="not-the-digest")

    def test_stale_preview_is_refused_and_deletes_nothing(self, tmp_git_repo):
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        victim = proj / ".agentic" / "inbox" / "ORPHAN-9001.md"
        victim.write_text("# orphan\n", encoding="utf-8")
        plan = api.preview_destruction(proj, "reset")
        # состояние сдвинулось после preview — план устарел
        state_store.publish_active_run(proj, {"generation": 1, "active": True})
        with pytest.raises(api.AwfApiError) as ei:
            api.execute_destruction(proj, plan, confirm=plan.digest)
        assert "stale" in str(ei.value).lower()
        assert victim.exists(), "устаревший план не должен ничего удалять"

    def test_successful_preview_execution(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        victim = proj / ".agentic" / "inbox" / "ORPHAN-9001.md"
        victim.write_text("# orphan\n", encoding="utf-8")
        plan = api.preview_destruction(proj, "reset")
        result = api.execute_destruction(
            proj, plan, confirm=plan.digest, decided_by="test:unit"
        )
        assert result.mode == "default"
        assert not victim.exists(), "подтверждённый план исполняется"

    def test_init_preview_scope_names_rewritten_skeleton(self, tmp_git_repo):
        """A-1: force-init перезаписывает config.yaml/supervisor.md/plan.md —
        подтверждающий digest обязан их видеть, а не только снос каталогов."""
        proj = _project(tmp_git_repo)
        plan = api.preview_destruction(proj, "init")
        joined = " ".join(plan.scope)
        assert "config.yaml" in joined, plan.scope
        assert "roles/supervisor.md" in joined, plan.scope
        assert "phases/plan.md" in joined, plan.scope


class TestCliPreview:
    """F-R2: ``awf reset --preview`` печатает план и ничего не удаляет."""

    def test_reset_preview_prints_and_does_not_delete(self, tmp_git_repo, capsys):
        from types import SimpleNamespace

        from awf import cmd_reset

        proj = _project(tmp_git_repo)
        victim = proj / ".agentic" / "inbox" / "ORPHAN-9001.md"
        victim.write_text("# orphan\n", encoding="utf-8")
        args = SimpleNamespace(
            tasks_only=False,
            full=False,
            orphans=False,
            force=False,
            preview=True,
            confirm_digest=None,
            project_dir=str(proj),
        )
        rc = cmd_reset.run(args)
        assert rc == 0
        out = capsys.readouterr().out.lower()
        assert "preview" in out and "confirm digest" in out
        assert victim.exists(), "preview не должен ничего удалять"


class TestExecutionMarkerPublished:
    """F-R1: захват публикует durable-метку ExecutionId.

    Тест намеренно использует ТОЛЬКО символы, существующие на baseline
    (``_lease.acquire``/``lease.release``), чтобы красный был настоящим
    ``assert``-ом, а не отсутствием символа.
    """

    def test_acquire_publishes_execution_marker(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lease = _lease.acquire(proj)
        try:
            marker = proj / ".agentic" / "logs" / "awf-launch.execution"
            assert marker.is_file(), (
                "acquire обязан опубликовать метку ExecutionId "
                "(awf-launch.execution)"
            )
            assert marker.read_text(encoding="utf-8").strip() != "", (
                "метка ExecutionId не должна быть пустой"
            )
        finally:
            lease.release()


# ─── REVIEW-фикс 9 (iter 12): финальные хвосты F-1..F-6 ──────────────────


class TestResetRotatesExecutionIdentity:
    """F-1: reset НЕ удаляет метку ExecutionId, а пишет свежий id.

    Удаление метки (старое поведение — цикл берёг только lease) снова
    открывало правило «метки нет = работай», и crash-orphan ребёнок
    старого запуска проходил handshake. Сохранение прежнего значения тоже
    не спасало: прошёл бы ровно тот orphan, чей id совпал. Свежий id
    отвергает любого прежнего, а файл остаётся на месте."""

    def test_reset_rotates_marker_and_refuses_orphan(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        lease = _lease.acquire(proj)
        old_id = lease.execution_id
        assert old_id, "acquire обязан выдать ExecutionId"
        lease.release()

        api.reset_runtime(proj)

        marker = proj / ".agentic" / "logs" / "awf-launch.execution"
        assert marker.is_file(), "метка ExecutionId обязана пережить reset"
        new_id = _lease.read_execution_id(proj)
        assert new_id, "метка не должна опустеть после reset"
        assert new_id != old_id, (
            "reset обязан СМЕНИТЬ идентичность запуска: прежнее значение "
            "пропустило бы orphan того же запуска"
        )
        assert _lease.execution_confirmed(proj, old_id) is False, (
            "orphan старого запуска не должен пройти handshake после reset"
        )
        assert _lease.execution_confirmed(proj, new_id) is True, (
            "свежая метка подтверждает соответствующий ей id"
        )

    def test_reset_creates_marker_when_absent(self, tmp_git_repo):
        """Даже без прежней метки reset её заводит: «файла нет = работай»
        больше не оставляет дверь открытой."""
        proj = _project(tmp_git_repo)
        marker = proj / ".agentic" / "logs" / "awf-launch.execution"
        if marker.exists():
            marker.unlink()
        api.reset_runtime(proj)
        assert _lease.read_execution_id(proj), (
            "после reset метка обязана существовать (свежий id)"
        )


class TestDestructiveNonPosixHumanText:
    """F-2: на не-POSIX разрушение отказывает ЧЕЛОВЕЧЕСКИМ текстом."""

    def test_reset_refusal_names_consequence_not_lowlevel(
        self, tmp_git_repo, monkeypatch
    ):
        from awf.infra import managed_fs

        proj = _project(tmp_git_repo)
        monkeypatch.setattr(managed_fs, "is_posix", lambda: False)
        with pytest.raises(api.AwfApiError) as ei:
            api.reset_runtime(proj)
        msg = str(ei.value)
        assert "reset" in msg.lower() and "POSIX" in msg, (
            f"отказ обязан объяснить последствие для reset: {msg!r}"
        )
        assert "no-follow file operations need POSIX" not in msg, (
            "сырой require_posix-текст не должен быть единственным объяснением"
        )


class TestInterlockSinglePlatformPredicate:
    """F-4: один предикат доступности flock (``require_posix``), не
    ``sys.platform`` в одном месте и ``os.name`` в другом."""

    def test_launch_section_checks_posix_on_any_platform(
        self, tmp_git_repo, monkeypatch
    ):
        import awf.api._interlock as il
        from awf.infra import managed_fs

        proj = _project(tmp_git_repo)
        ops: list[str] = []

        def _record(op):
            ops.append(op)
            raise managed_fs.ManagedPathError("simulated unsupported platform")

        monkeypatch.setattr(managed_fs, "require_posix", _record)
        # НЕ win32: старый код пропускал свою проверку и падал позже сырым
        # ImportError / чужим require_posix из open_managed.
        monkeypatch.setattr("sys.platform", "linux")
        with pytest.raises(api.AwfApiError):
            with il.launch_section(proj):
                pass
        assert any("interlock" in o for o in ops), (
            f"launch_section обязан сам звать require_posix на любой "
            f"платформе: {ops!r}"
        )


class TestResetPreviewNamesStore:
    """F-5: preview reset называет очистку Store-зеркала активного рана."""

    def test_preview_scope_names_store_clear(self, tmp_git_repo):
        proj = _project(tmp_git_repo)
        plan = api.preview_destruction(proj, "reset")
        joined = " ".join(plan.scope).lower()
        assert "store" in joined, (
            f"preview обязан назвать очистку Store: {plan.scope!r}"
        )


class TestRefusedStartKeepsGeneration:
    """F-6: отказной ``run_start`` не двигает durable-счётчик поколений."""

    def test_refused_start_does_not_bump_generation(self, tmp_git_repo):
        from awf.infra import state_store

        proj = _project(tmp_git_repo)
        _activate(proj, TID_FORCE)
        api.run_start(proj, queue=[TID_FORCE])
        gen = state_store.current_run_generation(proj)
        assert gen == 1

        with pytest.raises(api.AwfApiError) as ei:
            api.run_start(proj, queue=[TID_FORCE])
        assert "already active" in str(ei.value).lower(), str(ei.value)
        assert state_store.current_run_generation(proj) == gen, (
            "отказ не должен поднимать watermark поколений"
        )
        assert run_state.generation_of(run_state.read_run(proj)) == gen
