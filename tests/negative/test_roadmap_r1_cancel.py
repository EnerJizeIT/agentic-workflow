"""R1.2 (roadmap R1): отмена не теряет принятую запись.

Репро аудита S5-01 перенесено в автотесты. На исходной версии (до R1.2)
файл КРАСНЫЙ: отменённая MCP-операция освобождала mutex, её поток
дописывал TODO поверх принятой второй записи, и принятое изменение
исчезало из файла и из обеих резервных копий.

Инварианты:
1. Cancel A → успешный B → позднее окончание A: запись B сохранена
   (владение живёт до фактического конца операции, не до отмены корутины).
2. Запись устаревшей версии отвергается, а не пишется поверх чужой
   принятой (файловый lock + сравнение с прочитанным текстом).
3. Повтор операции по operation_id возвращает её фактический исход;
   неизвестный исход — recovery_required, а не повтор-дубль.

Инъекция барьера — через ``Path.write_text`` (резервная копия пишется
обеими версиями и до первой записи содержимого): так тест доказуемо
красный на базовой версии и зелёный после правки.
"""
from __future__ import annotations

import asyncio
import contextlib
import inspect
import pathlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf import api

_REAL_WRITE_TEXT = pathlib.Path.write_text


def _recovery_required():
    """The R1.2 exception, imported lazily: on the baseline tree the symbol
    does not exist yet, and a module-level import would break collection
    (a broken-runner red, not the assertion red this file must show)."""
    from awf._errors import OperationRecoveryRequired

    return OperationRecoveryRequired


def _project(repo: Path) -> Path:
    api.init_project(repo, project_name="R1Cancel")
    api.dispatch_todo(repo, "# TODO-0001 — cancel target\n\nScratch.\n")
    return repo


def _md(repo: Path) -> Path:
    return repo / ".agentic" / "inbox" / "TODO-0001.md"


def _wait_for(pred, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _install_first_backup_gate(monkeypatch):
    """Block the FIRST ``.bak-`` write_text (the backup before the content
    write); later calls pass. Returns (ready, release) events."""
    ready = threading.Event()
    release = threading.Event()
    state = {"fired": False}

    def gate(self, data, *a, **k):
        if not state["fired"] and ".bak-" in self.name:
            state["fired"] = True
            ready.set()
            release.wait(timeout=30)
        return _REAL_WRITE_TEXT(self, data, *a, **k)

    monkeypatch.setattr(pathlib.Path, "write_text", gate)
    return ready, release


def _has_operation_id() -> bool:
    return "operation_id" in inspect.signature(api.update_todo).parameters


def _journal(project: Path, operation_id: str) -> Path:
    """The normalised journal path for ``operation_id`` (F4: the leaf is a
    digest, so tests must ask the code instead of hard-coding the id)."""
    from awf.infra import state_store

    rec = state_store.op_path(project, operation_id)
    rec.parent.mkdir(parents=True, exist_ok=True)
    return rec


# ── 1. Cancel A → успешный B → позднее окончание A: B не потерян ────────


class TestCancelledUpdateKeepsNextWrite:
    def test_cancelled_call_does_not_clobber_later_update(
        self, tmp_git_repo: Path, monkeypatch
    ):
        project = _project(tmp_git_repo)
        md = _md(project)
        ready, release = _install_first_backup_gate(monkeypatch)

        async def scenario() -> dict:
            from agent_workflow_ui.tools import awf as awf_tools

            task_a = asyncio.ensure_future(
                awf_tools.awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append="MARKER-A\n",
                )
            )
            # A's worker thread reached the barrier (post-read, pre-write)
            loop = asyncio.get_running_loop()
            assert await loop.run_in_executor(None, ready.wait, 30), (
                "A never reached the pre-write barrier"
            )
            # the client cancels A's call; its worker thread keeps running
            task_a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_a

            # B: a fresh accepted update while A is still in flight
            task_b = asyncio.ensure_future(
                awf_tools.awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append="MARKER-B\n",
                )
            )
            await asyncio.sleep(0.2)  # let B reach the lock / write
            release.set()  # A's worker resumes and finishes late
            return await asyncio.wait_for(task_b, timeout=30)

        result_b = asyncio.run(scenario())
        assert result_b["status"] == "ok", result_b

        # A's late write must have settled before we judge the file
        assert _wait_for(lambda: "MARKER-A" in md.read_text(encoding="utf-8"))
        text = md.read_text(encoding="utf-8")
        assert "MARKER-B" in text, (
            f"the accepted update B was clobbered by the cancelled call A: "
            f"{text!r}"
        )


class TestCancelledCallRetriedByOperationId:
    def test_cancelled_call_replayed_by_same_id_applies_once(
        self, tmp_git_repo: Path, monkeypatch
    ):
        """Requirements 2+4+5 in one flow: a cancelled call keeps ownership
        until its worker truly finishes (it lands the write late); a retry
        with the SAME operation_id must REPLAY the recorded outcome — no
        second effect, no second backup. The retry is queued behind the
        cancelled call's still-held lock, so the ordering is deterministic."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        ready, release = _install_first_backup_gate(monkeypatch)

        async def scenario() -> dict:
            from agent_workflow_ui.tools import awf as awf_tools

            task_a = asyncio.ensure_future(
                awf_tools.awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append="MARKER-A\n",
                    operation_id="op-cancel-retry",
                )
            )
            loop = asyncio.get_running_loop()
            assert await loop.run_in_executor(None, ready.wait, 30), (
                "the cancelled call never reached the pre-write barrier"
            )
            task_a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task_a

            # retry the SAME id while the cancelled call still owns the lock
            task_c = asyncio.ensure_future(
                awf_tools.awf_todo_update(
                    todo_id="TODO-0001",
                    project_dir=str(project),
                    append="MARKER-A\n",
                    operation_id="op-cancel-retry",
                )
            )
            await asyncio.sleep(0.2)  # C queues behind A's lock
            release.set()  # A's worker resumes and finishes late
            return await asyncio.wait_for(task_c, timeout=30)

        res = asyncio.run(scenario())
        assert res["status"] == "ok", res

        assert _wait_for(lambda: "MARKER-A" in md.read_text(encoding="utf-8"))
        text = md.read_text(encoding="utf-8")
        assert text.count("MARKER-A") == 1, (
            f"the cancelled call plus its retry applied twice: {text!r}"
        )
        baks = sorted(
            p.name for p in (project / ".agentic" / "context").glob("*.bak-*")
        )
        assert len(baks) == 1, (
            f"the retry of a cancelled call re-applied its effect "
            f"(a second backup exists): {baks}"
        )


# ── 2. Устаревшая версия отвергается (файловый lock + CAS) ──────────────


class TestStaleWriteRefused:
    def test_stale_write_is_refused_and_leaves_no_orphan(
        self, tmp_git_repo, monkeypatch
    ):
        """F1/F2: the stale writer is parked deterministically — not by a
        scheduler-dependent barrier — AFTER it wrote its (orphan-candidate)
        backup and BEFORE its guarded write. The winner applies meanwhile;
        the stale writer must be version-refused and its backup removed, so
        exactly the accepted writer's one ``.bak-`` survives."""
        if not _has_operation_id():
            pytest.fail(
                "update_todo has no guarded read-modify-write (R1.2) — a "
                "stale write overwrites a newer accepted one"
            )
        import awf.api.hygiene as hygiene

        project = _project(tmp_git_repo)
        md = _md(project)

        real = hygiene._write_todo_checked
        ready = threading.Event()
        release = threading.Event()
        state = {"fired": False}

        def gate(project_dir, path, expected_text, new_text, **kw):
            if not state["fired"]:
                state["fired"] = True
                ready.set()
                release.wait(timeout=30)
            return real(project_dir, path, expected_text, new_text, **kw)

        monkeypatch.setattr(hygiene, "_write_todo_checked", gate)

        outcomes: dict[str, object] = {}

        def stale_worker() -> None:
            try:
                api.update_todo(project, "TODO-0001", append="MARKER-A\n")
            except Exception as e:  # noqa: BLE001 — the refusal is the point
                outcomes["A"] = e
            else:
                outcomes["A"] = None

        t = threading.Thread(target=stale_worker)
        t.start()
        assert ready.wait(30), "the stale writer never reached the pre-write gate"

        # the accepted writer lands its update while A is parked
        api.update_todo(project, "TODO-0001", append="MARKER-B\n")
        release.set()  # A resumes into the now-stale version check
        t.join(timeout=60)

        text = md.read_text(encoding="utf-8")
        assert isinstance(outcomes.get("A"), api.AwfApiError), (
            f"the stale write was not version-refused: {outcomes}"
        )
        assert "MARKER-B" in text and "MARKER-A" not in text, (
            f"the accepted update B was clobbered by the stale write: {text!r}"
        )
        # T1(b)/F1: the refused writer's backup must not survive as an orphan
        # — exactly the accepted writer's one ``.bak-`` is left.
        baks = sorted(
            p.name for p in (project / ".agentic" / "context").glob("*.bak-*")
        )
        assert len(baks) == 1, (
            f"a refused write left an orphan backup (T1b): {baks}"
        )


class TestOperationIdHoldsLockAcrossRmw:
    def test_op_id_writer_blocks_plain_writer_then_refuses_it(
        self, tmp_git_repo, monkeypatch
    ):
        """The durable-operation writer holds the project lock for the WHOLE
        read-modify-write, so a concurrent plain writer is ordered after it
        and its stale version is refused (not written over)."""
        project = _project(tmp_git_repo)
        md = _md(project)
        op_ready = threading.Event()
        op_release = threading.Event()
        plain_backup = threading.Event()
        seen = {"n": 0}
        real = pathlib.Path.write_text

        def gate(self, data, *a, **k):
            if ".bak-" in self.name:
                seen["n"] += 1
                if seen["n"] == 1:
                    op_ready.set()
                    op_release.wait(timeout=30)
                elif seen["n"] == 2:
                    plain_backup.set()
            return real(self, data, *a, **k)

        monkeypatch.setattr(pathlib.Path, "write_text", gate)
        outcomes: dict[str, object] = {}

        def op_worker() -> None:
            try:
                api.update_todo(
                    project, "TODO-0001", append="OP\n", operation_id="op-lock"
                )
                outcomes["op"] = None
            except Exception as e:  # noqa: BLE001 — a raise is a test signal
                outcomes["op"] = e

        def plain_worker() -> None:
            try:
                api.update_todo(project, "TODO-0001", append="PLAIN\n")
                outcomes["plain"] = None
            except Exception as e:  # noqa: BLE001
                outcomes["plain"] = e

        t_op = threading.Thread(target=op_worker)
        t_op.start()
        assert op_ready.wait(30), "operation writer never reached its backup gate"
        t_plain = threading.Thread(target=plain_worker)
        t_plain.start()
        assert plain_backup.wait(30), "plain writer never reached its backup write"
        op_release.set()
        t_op.join(timeout=60)
        t_plain.join(timeout=60)

        text = md.read_text(encoding="utf-8")
        assert outcomes.get("op") is None, f"operation writer failed: {outcomes}"
        assert isinstance(outcomes.get("plain"), api.AwfApiError), (
            f"the plain writer was not refused by the version check: {outcomes}"
        )
        assert "OP" in text and "PLAIN" not in text, (
            f"the file lock did not serialize the operation and plain "
            f"writers: {text!r}"
        )


# ── 3. operation_id: replay + recovery_required ─────────────────────────


class TestOperationId:
    def test_replay_returns_recorded_outcome(self, tmp_git_repo):
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)

        first = api.update_todo(
            project, "TODO-0001", append="MARKER-ONE\n", operation_id="op-one"
        )
        text_after_first = md.read_text(encoding="utf-8")
        assert "MARKER-ONE" in text_after_first

        second = api.update_todo(
            project, "TODO-0001", append="MARKER-ONE\n", operation_id="op-one"
        )
        text_after_second = md.read_text(encoding="utf-8")
        assert text_after_second == text_after_first, (
            "a replay of the same operation applied a second time"
        )
        assert second.backup == first.backup

    def test_unknown_outcome_is_recovery_required(self, tmp_git_repo, monkeypatch):
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        import awf.api.hygiene as hygiene

        def boom(*a, **k):
            raise RuntimeError("simulated crash mid-write")

        monkeypatch.setattr(hygiene, "_write_todo_checked", boom)
        with pytest.raises(RuntimeError):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n", operation_id="op-crash"
            )
        monkeypatch.undo()

        # the attempt started but recorded no finish → outcome is unknown
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n", operation_id="op-crash"
            )

    def test_corrupt_journal_is_recovery_required(self, tmp_git_repo):
        """Stage-3 F2: an unreadable journal means the outcome is UNKNOWN,
        not 'never started' — a retry must not silently re-apply it."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        _journal(project, "op-broken").write_text("{not valid json", encoding="utf-8")

        before = md.read_text(encoding="utf-8")
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n",
                operation_id="op-broken",
            )
        assert md.read_text(encoding="utf-8") == before, (
            "a corrupt journal let the retry re-apply the write"
        )

    def test_refused_operation_replays_the_refusal(self, tmp_git_repo):
        """Requirement 4 'refused': a retry of a refused op id re-raises the
        same refusal and never applies a second effect."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        before = md.read_text(encoding="utf-8")

        # two content modes at once — a deterministic refusal, no write
        with pytest.raises(api.AwfApiError) as first:
            api.update_todo(
                project, "TODO-0001", content="x", append="y",
                operation_id="op-refused",
            )
        with pytest.raises(api.AwfApiError) as replay:
            api.update_todo(
                project, "TODO-0001", content="x", append="y",
                operation_id="op-refused",
            )
        assert str(replay.value) == str(first.value), (
            "the retry did not replay the recorded refusal"
        )
        assert md.read_text(encoding="utf-8") == before

    def test_invalid_operation_id_is_refused(self, tmp_git_repo):
        """An operation id is a durable token — a traversal-ish, empty or
        over-long id is refused before any effect."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        before = md.read_text(encoding="utf-8")
        for bad in ("../escape", "", "a/b", "a" * 66, ".hidden"):
            with pytest.raises(api.AwfApiError):
                api.update_todo(
                    project, "TODO-0001", append="MARKER-X\n",
                    operation_id=bad,
                )
        assert md.read_text(encoding="utf-8") == before

    def test_mcp_reports_recovery_required_outcome(self, tmp_git_repo):
        """Stage-3 F1: 'do not retry blindly' must reach the MCP caller as a
        machine-readable outcome, not only as prose in the error string."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        _journal(project, "op-broken").write_text("{broken", encoding="utf-8")

        async def call() -> dict:
            from agent_workflow_ui.tools import awf as awf_tools

            return await awf_tools.awf_todo_update(
                todo_id="TODO-0001",
                project_dir=str(project),
                append="MARKER-X\n",
                operation_id="op-broken",
            )

        res = asyncio.run(call())
        assert res["status"] == "error", res
        assert res.get("outcome") == "recovery_required", res


# ── F4: имя журнала нормализовано (зарезервированные имена, регистр) ─────


class TestOperationJournalFilename:
    def test_journal_leaf_is_a_digest_not_the_raw_id(self, tmp_git_repo):
        """F4: the id never becomes the file leaf — a Windows reserved
        device name (``con``/``nul``/``aux``) would be unreadable there and
        silently degrade the journal guarantee to 'never started'."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        for op in ("plain", "con", "NUL", "aux"):
            leaf = _journal(project, op).name
            assert leaf != f"{op}.json", (
                f"operation id {op!r} was used verbatim as the journal leaf"
            )
            assert leaf.lower() not in {"con.json", "nul.json", "aux.json"}, leaf

    def test_case_different_ids_do_not_collide(self, tmp_git_repo):
        """F4: a case-insensitive filesystem must not merge ``Op-1`` /
        ``op-1`` into one journal (one operation replaying another's)."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        upper = api.update_todo(
            project, "TODO-0001", append="MARKER-UP\n", operation_id="Op-1"
        )
        lower = api.update_todo(
            project, "TODO-0001", append="MARKER-LOW\n", operation_id="op-1"
        )
        assert upper.backup != lower.backup, "two operations replayed as one"
        text = md.read_text(encoding="utf-8")
        assert "MARKER-UP" in text and "MARKER-LOW" in text, text

    def test_reserved_device_id_still_replays(self, tmp_git_repo):
        """F4: a reserved-name id journals and replays — the leaf is safe,
        the real id lives in the record body."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        first = api.update_todo(
            project, "TODO-0001", append="MARKER-CON\n", operation_id="con"
        )
        second = api.update_todo(
            project, "TODO-0001", append="MARKER-CON\n", operation_id="con"
        )
        assert second.backup == first.backup, "the retry did not replay"
        assert md.read_text(encoding="utf-8").count("MARKER-CON") == 1


# ── REVIEW-фикс (iter 2): журнал — это гарантия, а не украшение ─────────
#
# F-HIGH: ``_write_operation`` глотала OSError, поэтому неудачная ПЕРВАЯ
# запись журнала не мешала операции примениться (повтор по operation_id
# читал «нет файла» → None → применял заново — дубль). Стартовая запись
# обязана отказать ДО эффекта; неудача финальной — вернуть
# recovery_required, а не молчать.


def _fail_journal_writes(monkeypatch, *, fail_start=True, fail_finish=False):
    """Inject OSError into the operation-journal writes only (the
    ``state/operations/`` path), leaving the TODO write untouched.

    R1.5: the journal writer moved into the StateStore
    (``state_store.write_operation``) — hygiene no longer touches the file
    bytes itself. The injection point follows the owner: patch the Store
    writer, not ``hygiene.atomic_write_text``.
    """
    from awf.infra import state_store

    real = state_store.write_operation
    calls = {"n": 0}

    def fake(project_dir, operation_id, **fields):
        calls["n"] += 1
        n = calls["n"]
        if (n == 1 and fail_start) or (n == 2 and fail_finish):
            raise OSError(28, "No space left on device (injected)")
        return real(project_dir, operation_id, **fields)

    monkeypatch.setattr(state_store, "write_operation", fake)
    return calls


class TestFinishJournalFailure:
    def test_start_journal_failure_refuses_before_apply(
        self, tmp_git_repo, monkeypatch
    ):
        project = _project(tmp_git_repo)
        md = _md(project)
        before = md.read_text(encoding="utf-8")
        _fail_journal_writes(monkeypatch, fail_start=True)

        with pytest.raises(Exception):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n",
                operation_id="op-nostart",
            )
        assert md.read_text(encoding="utf-8") == before, (
            "the operation applied although its start journal could not be "
            "written (a retry would then re-apply it — duplicate)"
        )

    def test_finish_journal_failure_returns_recovery_required(
        self, tmp_git_repo, monkeypatch
    ):
        project = _project(tmp_git_repo)
        md = _md(project)
        _fail_journal_writes(monkeypatch, fail_start=False, fail_finish=True)

        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n",
                operation_id="op-nofinish",
            )
        monkeypatch.undo()

        assert "MARKER-X" in md.read_text(encoding="utf-8")
        # a retry reads the open ``started`` record → unknown, not a re-apply
        before = md.read_text(encoding="utf-8")
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n",
                operation_id="op-nofinish",
            )
        assert md.read_text(encoding="utf-8") == before


# ── F-MED-1: check→start→finish журнала под файловым lock ───────────────


class TestSameOperationIdConcurrency:
    def test_two_concurrent_calls_with_one_id_apply_once(
        self, tmp_git_repo, monkeypatch
    ):
        project = _project(tmp_git_repo)
        md = _md(project)
        ready, release = _install_first_backup_gate(monkeypatch)

        outcomes: dict[str, object] = {}

        def worker(name: str) -> None:
            try:
                outcomes[name] = api.update_todo(
                    project, "TODO-0001", append=f"MARKER-{name}\n",
                    operation_id="op-same",
                )
            except Exception as e:  # noqa: BLE001 — a raise is a test signal
                outcomes[name] = e

        t1 = threading.Thread(target=worker, args=("A",))
        t1.start()
        assert ready.wait(30), "the first call never reached the pre-write gate"
        t2 = threading.Thread(target=worker, args=("B",))
        t2.start()
        release.set()
        t1.join(timeout=60)
        t2.join(timeout=60)

        text = md.read_text(encoding="utf-8")
        assert text.count("MARKER-") == 1, (
            f"one operation_id applied twice (duplicate effect): {text!r}"
        )
        assert not any(isinstance(v, Exception) for v in outcomes.values()), (
            f"a concurrent same-id call failed instead of replaying: {outcomes}"
        )
        first, second = outcomes["A"], outcomes["B"]
        assert first.backup == second.backup, (
            "the second call did not replay the recorded outcome"
        )


# ── F-MED-2: проверяемая запись на всех read-modify-write операциях ─────


class TestHygieneMutationsUseLock:
    @pytest.mark.parametrize("op", ["remove", "retire", "unblock"])
    def test_all_rmw_todo_ops_take_the_project_lock(
        self, tmp_git_repo, monkeypatch, op
    ):
        import awf._lock as lock_mod

        project = _project(tmp_git_repo)
        if op == "unblock":
            outbox = project / ".agentic" / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / "BLOCKED-TODO-0001.ready").write_text("x", encoding="utf-8")

        calls: list[Path] = []
        real_locked = lock_mod.locked

        def spy(project_dir):
            calls.append(project_dir)
            return real_locked(project_dir)

        monkeypatch.setattr(lock_mod, "locked", spy)

        if op == "remove":
            api.remove_todo(project, "TODO-0001")
        elif op == "retire":
            api.retire_todo(project, "TODO-0001", reason="r1.2 lock guard")
        else:
            api.unblock_todo(project, "TODO-0001")

        assert calls, (
            f"{op} did a read-modify-write without the project lock "
            "(requirement 3: TODO RMW ops serialize)"
        )


# ── F-MED-3: operation_id доступен не только через MCP ──────────────────


class TestCliOperationId:
    def test_todo_update_parser_has_operation_id(self, tmp_git_repo):
        from awf.cli import _build_parser

        parser, _ = _build_parser()
        args = parser.parse_args(
            [
                "todo-update", "TODO-0001", "--content", "x",
                "--operation-id", "op-cli",
                "--project-dir", str(tmp_git_repo),
            ]
        )
        assert args.operation_id == "op-cli"

    def test_cmd_todo_update_forwards_operation_id(self, tmp_git_repo, monkeypatch):
        from awf import cmd_todo_update

        seen: dict[str, object] = {}

        class _Res:
            message = "ok"

        def fake(*a, **k):
            seen.update(k)
            return _Res()

        monkeypatch.setattr(cmd_todo_update.api, "update_todo", fake)
        args = SimpleNamespace(
            project_dir=str(tmp_git_repo),
            todo_id="TODO-0001",
            reason="",
            content="x",
            content_file="",
            operation_id="op-cli",
        )
        assert cmd_todo_update.run(args) == 0
        assert seen.get("operation_id") == "op-cli"

    def test_cmd_todo_update_reports_recovery_required(
        self, tmp_git_repo, monkeypatch, capsys
    ):
        """F3: an unknown outcome (recovery_required) must reach the CLI as
        a distinct exit code and a machine token, not be flattened into a
        plain refusal an automated script would blindly retry."""
        from awf import cmd_todo_update
        from awf._errors import OperationRecoveryRequired

        def boom(*a, **k):
            raise OperationRecoveryRequired("op-cli")

        monkeypatch.setattr(cmd_todo_update.api, "update_todo", boom)
        args = SimpleNamespace(
            project_dir=str(tmp_git_repo),
            todo_id="TODO-0001",
            reason="",
            content="x",
            content_file="",
            operation_id="op-cli",
        )
        assert cmd_todo_update.run(args) == 2
        out = capsys.readouterr().out
        assert "recovery_required" in out


# ── Stage-3 design review: a half-written journal is unknown, never a guess ─
#
# AR-A: a recorded ``applied`` without a complete result must not replay as a
# hollow ok (empty backup/message) — that is the "outcome on luck" requirement
# 4 forbids. AR-B: the body's ``operation_id`` is the F4 rationale's only
# copy of the real id — a record naming another id must not be trusted.


class TestMalformedJournalIsNeverGuessed:
    def test_applied_record_without_result_is_recovery_required(self, tmp_git_repo):
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        _journal(project, "op-half").write_text(
            '{"status": "applied", "operation_id": "op-half"}', encoding="utf-8"
        )
        before = md.read_text(encoding="utf-8")
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n", operation_id="op-half"
            )
        assert md.read_text(encoding="utf-8") == before

    def test_journal_body_id_mismatch_is_recovery_required(self, tmp_git_repo):
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)
        _journal(project, "op-mine").write_text(
            '{"status": "applied", "operation_id": "op-other", '
            '"result": {"todo_id": "TODO-0001", "backup": "b", "message": "m"}}',
            encoding="utf-8",
        )
        before = md.read_text(encoding="utf-8")
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0001", append="MARKER-X\n", operation_id="op-mine"
            )
        assert md.read_text(encoding="utf-8") == before


# ── iter-4 AUD-1: реплей связан с запрошенной целью (todo_id + action) ───
#
# Один operation_id называет ОДНУ операцию. Переиспользование его для другой
# единицы не должно отдавать результат первой как «применено» и не должно
# трогать вторую единицу — это чужой исход под своим id.


class TestReplayBindsToRequestedTarget:
    def test_replay_with_different_target_is_refused_not_foreign_result(
        self, tmp_git_repo
    ):
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        api.dispatch_todo(project, "# TODO-0002 — second target\n\nScratch.\n")
        md_one = _md(project)
        md_two = project / ".agentic" / "inbox" / "TODO-0002.md"
        assert md_two.is_file(), "the second TODO was not dispatched"

        api.update_todo(
            project, "TODO-0001", append="MARKER-ONE\n", operation_id="op-shared"
        )
        assert "MARKER-ONE" in md_one.read_text(encoding="utf-8")
        text_two_before = md_two.read_text(encoding="utf-8")

        # the SAME id, now aimed at another unit — the stored outcome of the
        # FIRST operation must not be handed back as if the second applied
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0002", append="MARKER-TWO\n",
                operation_id="op-shared",
            )
        assert md_two.read_text(encoding="utf-8") == text_two_before, (
            "a reused operation_id wrote to the second unit"
        )
        assert "MARKER-TWO" not in md_two.read_text(encoding="utf-8")


# ── iter-4 AUD-2: проверка «pipeline running» под тем же файловым lock ───


class TestPipelineGuardUnderLock:
    @pytest.mark.parametrize("op", ["remove", "retire", "unblock", "update"])
    def test_pipeline_running_guard_runs_under_the_project_lock(
        self, tmp_git_repo, monkeypatch, op
    ):
        """AUD-2: remove_todo and unblock_todo used to consult the pipeline
        liveness BEFORE taking the project lock — the window between check
        and move let a concurrent ``awf start`` slip in. All four RMW ops
        must consult it while holding the lock."""
        import awf._lock as lock_mod
        import awf.api.hygiene as hygiene

        project = _project(tmp_git_repo)
        if op == "unblock":
            outbox = project / ".agentic" / "outbox"
            outbox.mkdir(parents=True, exist_ok=True)
            (outbox / "BLOCKED-TODO-0001.ready").write_text("x", encoding="utf-8")

        held = {"depth": 0}
        seen: list[bool] = []
        real_locked = lock_mod.locked

        @contextlib.contextmanager
        def spy_locked(project_dir):
            with real_locked(project_dir):
                held["depth"] += 1
                try:
                    yield
                finally:
                    held["depth"] -= 1

        def spy_check(project_dir, **kwargs):
            seen.append(held["depth"] > 0)
            return False, None, None

        monkeypatch.setattr(lock_mod, "locked", spy_locked)
        monkeypatch.setattr(hygiene, "check_pipeline_running", spy_check)

        if op == "remove":
            api.remove_todo(project, "TODO-0001")
        elif op == "retire":
            api.retire_todo(project, "TODO-0001", reason="aud-2 guard")
        elif op == "update":
            api.update_todo(
                project, "TODO-0001", append="MARKER-AUD2\n",
                operation_id="op-aud2",
            )
        else:
            api.unblock_todo(project, "TODO-0001")

        assert seen, f"{op} never consulted the pipeline-running guard"
        assert all(seen), (
            f"{op} checked 'pipeline running' OUTSIDE the project lock — a "
            "concurrent awf start can slip between the check and the move "
            "(AUD-2)"
        )


# ── QA (stage agent-qa-review, iter-5): границы operation_id и связь ──────
# реплея с целью на пути ОТКАЗА (не только applied).


class TestOperationIdBoundaries:
    def test_valid_64_char_id_is_accepted_and_65_is_refused(self, tmp_git_repo):
        """T1(a) guard: the accept/reject boundary is exactly 64 chars — the
        regex ``{0,63}`` after the first char and the "1..64" message used to
        disagree by one. A 64-char id must journal and apply; a 65-char one
        must be refused before any effect."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        md = _md(project)

        api.update_todo(
            project, "TODO-0001", append="MARKER-64\n", operation_id="a" * 64
        )
        assert "MARKER-64" in md.read_text(encoding="utf-8")
        before = md.read_text(encoding="utf-8")

        with pytest.raises(api.AwfApiError):
            api.update_todo(
                project, "TODO-0001", append="MARKER-65\n",
                operation_id="a" * 65,
            )
        assert md.read_text(encoding="utf-8") == before

    def test_refused_record_reused_for_another_target_is_recovery_required(
        self, tmp_git_repo
    ):
        """AUD-1 on the REFUSAL path (previously only the applied path was
        covered): a refused operation_id reused for a different unit must
        not hand back the first operation's refusal as if it named the
        second — the target binding must fire regardless of the recorded
        status, and the second unit's text stays untouched."""
        if not _has_operation_id():
            pytest.fail("update_todo has no operation_id (R1.2 durable state)")
        project = _project(tmp_git_repo)
        api.dispatch_todo(project, "# TODO-0002 — second target\n\nScratch.\n")
        md_two = project / ".agentic" / "inbox" / "TODO-0002.md"
        before_two = md_two.read_text(encoding="utf-8")

        # a deterministic refusal (two content modes) recorded as "refused"
        with pytest.raises(api.AwfApiError):
            api.update_todo(
                project, "TODO-0001", content="x", append="y",
                operation_id="op-refused-shared",
            )
        # the SAME id aimed at another unit → unknown, never the first refusal
        with pytest.raises(_recovery_required()):
            api.update_todo(
                project, "TODO-0002", append="MARKER-TWO\n",
                operation_id="op-refused-shared",
            )
        assert md_two.read_text(encoding="utf-8") == before_two, (
            "a refused operation_id reused for another unit wrote to it"
        )
