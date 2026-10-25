"""M3, виток 2 (REVIEW TODO-0227): F1 — один барьер для JSON-писателя Store,
F2 — тесты по классу S9-03 (симлинк самого ``.agentic``) и скан литералов.

Все проекты — disposable в ``tmp_path``; canary лежат ВНЕ проекта.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

T = "TODO-0001"

REPO_ROOT = Path(__file__).resolve().parents[2]


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "logs", "done", "state"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "inbox" / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text("", encoding="utf-8")
    (project / ".agentic" / "outbox" / f"DONE-{T}.md").write_text("done\n", encoding="utf-8")
    (project / ".agentic" / "outbox" / f"DONE-{T}.ready").write_text("", encoding="utf-8")
    return project


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=target.is_dir())


def _agentic_symlink(tmp_path: Path) -> tuple[Path, Path]:
    """``proj/.agentic`` → ``real/.agentic``: возвращает (proj, real)."""
    real = _project(tmp_path / "real")
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".agentic").symlink_to(
        real / ".agentic", target_is_directory=True
    )
    return project, real


# ─── F1: JSON-писатель Store под managed-барьером ───────────────────────


def test_write_operation_refuses_symlinked_state(tmp_path):
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(project / ".agentic" / "state", outside)

    with pytest.raises(ManagedPathError):
        state_store.write_operation(project, "op-out", status="reserved")

    assert not (outside / "operations").exists()


def test_write_operation_refuses_symlinked_agentic(tmp_path):
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    project, real = _agentic_symlink(tmp_path)
    with pytest.raises(ManagedPathError):
        state_store.write_operation(project, "op-out", status="reserved")
    assert not (real / ".agentic" / "state" / "operations").exists()


def test_put_record_and_control_refuse_symlinked_state(tmp_path):
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(project / ".agentic" / "state", outside)

    with pytest.raises(ManagedPathError):
        state_store.put_record(project, "kind", {"a": 1})
    with pytest.raises(ManagedPathError):
        state_store.atomic_write_json(state_store.control_path(project), {"x": 1})

    assert not (outside / "records").exists()
    assert not (outside / "control.json").exists()


def test_read_operation_refuses_external_applied(tmp_path):
    """Симлинк → чужой ``applied`` не читается как исход этой операции."""
    from awf._errors import OperationRecoveryRequired
    from awf.infra import state_store

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    (outside / "operations").mkdir(parents=True)
    path = outside / "operations" / f"{state_store.sha256('op-ext')}.json"
    path.write_text(
        json.dumps({"operation_id": "op-ext", "status": "applied"}),
        encoding="utf-8",
    )
    _replace_with_symlink(project / ".agentic" / "state", outside)

    with pytest.raises(OperationRecoveryRequired):
        state_store.read_operation(project, "op-ext")


def test_read_operation_missing_is_still_never_started(tmp_path):
    from awf.infra import state_store

    project = _project(tmp_path)
    assert state_store.read_operation(project, "op-none") is None


# ─── F2: класс S9-03 — симлинком является сам ``.agentic`` ──────────────


def test_wipe_refuses_symlinked_agentic_and_keeps_canary(tmp_path):
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    project, real = _agentic_symlink(tmp_path)
    canary = real / ".agentic" / "state" / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")

    with pytest.raises(ManagedPathError):
        state_store.delete_state_tree(project / ".agentic" / "state")

    assert canary.read_text(encoding="utf-8") == "keep me"


def test_archive_refuses_symlinked_agentic_before_any_move(tmp_path):
    from awf import todos

    project, real = _agentic_symlink(tmp_path)

    with pytest.raises(OSError):
        todos.archive_todo(project, T)

    assert not (real / ".agentic" / "done" / T).exists()
    assert (real / ".agentic" / "inbox" / f"{T}.md").is_file()
    assert (real / ".agentic" / "outbox" / f"DONE-{T}.md").is_file()


def test_lock_file_refuses_symlinked_agentic(tmp_path):
    from awf import run_state
    from awf.infra.managed_fs import ManagedPathError

    project, real = _agentic_symlink(tmp_path)
    with pytest.raises(ManagedPathError):
        run_state._ensure_lock_file(project)
    assert not (real / ".agentic" / "state" / ".lock").exists()


def test_catch_list_is_imported_not_respelled():
    """Литерал-кортеж рядом с импортом валит тест, не только equality имён."""
    literal = re.compile(
        r"\(\s*(?:managed_fs\.)?ManagedPathError\s*,[^)]*LockUnavailable"
        r"|LockUnavailable\s*,[^)]*(?:managed_fs\.)?ManagedPathError"
    )
    offenders = [
        p.name
        for p in (REPO_ROOT / "awf").rglob("*.py")
        if p.name != "__init__.py"
        and literal.search(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], offenders


# ─── F-AR2 (architect-review): источники архива — тот же барьер ─────────


def test_archive_refuses_symlinked_inbox_source(tmp_path):
    """F-AR2: симлинк ``.agentic/inbox`` уводит уборку источников наружу.

    Барьер ``done/`` не спасает: ``unlink`` потреблённых сигналов и
    ``shutil.move`` источников идут из ``inbox``/``outbox``. Один preflight
    по всем четырём поверхностям до первого чтения/побочного эффекта.
    """
    from awf import todos

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / f"{T}.md"
    victim.write_text("# foreign\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "inbox", outside)

    with pytest.raises(OSError):
        todos.archive_todo(project, T)

    assert victim.read_text(encoding="utf-8") == "# foreign\n"
    assert not (project / ".agentic" / "done" / T).exists()


def test_archive_refuses_symlinked_outbox_source(tmp_path):
    from awf import todos

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / f"DONE-{T}.md"
    victim.write_text("done\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "outbox", outside)

    with pytest.raises(OSError):
        todos.archive_todo(project, T)

    assert victim.read_text(encoding="utf-8") == "done\n"
    assert not (project / ".agentic" / "done" / T).exists()


# ─── F-AR1 (architect-review): журнал — одна семья отказов ──────────────


def test_update_todo_finish_write_managed_error_is_recovery_required(
    tmp_path, monkeypatch
):
    """F-AR1: ``ManagedPathError`` на finish-записи — не сырое исключение.

    Писатель журнала теперь под managed-барьером, поэтому его семья отказов
    шире ``OSError``. Документированный контракт «эффект лёг, исход не
    записан → recovery_required» обязан покрывать и этот класс.
    """
    from awf._errors import OperationRecoveryRequired
    from awf.api import hygiene
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    monkeypatch.setattr(hygiene, "_read_operation", lambda *a, **k: None)

    real = hygiene._write_operation
    calls = {"n": 0}

    def flaky(proj, operation_id, **fields):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(proj, operation_id, **fields)
        raise ManagedPathError("state component swapped mid-operation")

    monkeypatch.setattr(hygiene, "_write_operation", flaky)
    monkeypatch.setattr(
        hygiene,
        "_update_todo_impl",
        lambda *a, **k: hygiene.UpdateTodoResult(
            todo_id=T, backup=f"context/{T}.md.bak-x", message="ok"
        ),
    )

    with pytest.raises(OperationRecoveryRequired):
        hygiene.update_todo(project, T, content="# new\n", operation_id="op-finish")


# ─── Добор QA (виток 2): fsync-гарантия и round-trip читателей ───────────


def test_managed_write_durable_fsyncs_file_and_directory(tmp_path, monkeypatch):
    """F1: ``durable=True`` обязан fsync'ить И файл, И каталог.

    Соседний тест проверяет только переданный флаг: если сами вызовы
    ``fsync`` убрать, «durable» станет надписью, а строка журнала снова
    начнёт теряться при падении хоста — эффект применится дважды.
    """
    import os
    import stat

    from awf.infra import managed_fs

    calls: list[str] = []
    real = os.fsync

    def spy(fd):
        calls.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real(fd)

    monkeypatch.setattr(managed_fs.os, "fsync", spy)
    root = tmp_path / "proj"
    root.mkdir()
    path = root / ".agentic" / "state" / "operation.json"

    managed_fs.managed_write_text(path, "{}\n", root=root, durable=True)
    assert calls == ["file", "dir"], calls

    calls.clear()
    managed_fs.managed_write_text(path, "{}\n", root=root)
    assert calls == [], "a non-durable write must not fsync"


def test_journal_record_reads_back_through_both_readers(tmp_path):
    """(e) round-trip: строку каждого писателя читает ОДИН читатель.

    Store-запись читается через hygiene, hygiene-запись — через Store;
    статус канонический в обе стороны (legacy ``started`` → ``running``).
    """
    from awf.api import hygiene
    from awf.infra import state_store

    project = _project(tmp_path)

    state_store.write_operation(project, "op-store", status="applied", todo_id=T)
    assert hygiene._read_operation(project, "op-store")["status"] == "applied"

    hygiene._write_operation(project, "op-hyg", status="started", todo_id=T)
    assert state_store.read_operation(project, "op-hyg")["status"] == "running"
