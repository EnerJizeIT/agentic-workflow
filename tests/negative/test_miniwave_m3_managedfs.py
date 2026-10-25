"""M3 (BACKLOG, мини-волна): ManagedFS-хвост + единая схема журнала.

Приёмка TODO-0227 (a-f). На исходном дереве файл КРАСНЫЙ:

(a) ``state_store.delete_state_tree`` ходит по ``.agentic/state`` без
    no-follow preflight — симлинк ``state`` уводит ``iterdir``/``unlink``
    наружу проекта, canary снаружи гибнет;
(b) ``todos.archive_todo`` переносит ``done/`` без managed-барьера —
    симлинк ``.agentic/done`` уводит архив наружу ДО отказа;
(c) ``run_state._ensure_lock_file`` принимает симлинк-leaf за готовый
    lock-файл (``is_file()`` следует по ссылке);
(d) catch-лист «no state» — один список на писателей/уборщиков, тест на
    каждый класс (OSError / ManagedPathError / LockUnavailable);
(e) журнал операций — один владелец схемы (statuses/ts/args_digest,
    indent=2), Store и hygiene пишут одинаково, чужой формат —
    recovery_required;
(f) ``pipeline_state`` создаёт ``state/`` сырым ``mkdir`` — висячий
    симлинк роняет функцию вместо деградации «no state».

Все проекты — disposable в ``tmp_path``; canary-файлы лежат ВНЕ проекта.
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

T = "TODO-0001"

REPO_ROOT = Path(__file__).resolve().parents[2]
ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "logs", "done", "state"):
        (project / ".agentic" / name).mkdir(parents=True)
    (project / ".agentic" / "inbox" / f"{T}.md").write_text(
        "# task\n", encoding="utf-8"
    )
    (project / ".agentic" / "inbox" / f"{T}.ready").write_text(
        "", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / f"DONE-{T}.md").write_text(
        "done\n", encoding="utf-8"
    )
    (project / ".agentic" / "outbox" / f"DONE-{T}.ready").write_text(
        "", encoding="utf-8"
    )
    return project


def _replace_with_symlink(path: Path, target: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=target.is_dir())


# ─── (a): wipe через симлинк ────────────────────────────────────────────


def test_wipe_refuses_symlinked_state_and_keeps_outside_canary(tmp_path):
    """Симлинк ``.agentic/state`` → отказ ДО удаления; canary снаружи цел."""
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")

    state = project / ".agentic" / "state"
    _replace_with_symlink(state, outside)

    with pytest.raises(ManagedPathError):
        state_store.delete_state_tree(state)

    assert canary.read_text(encoding="utf-8") == "keep me"


def test_reset_does_not_delete_the_lock_file(tmp_path):
    """Свойство «reset не трогает lock» остаётся при managed-барьере."""
    from awf.infra import state_store

    project = _project(tmp_path)
    state = project / ".agentic" / "state"
    (state / ".lock").write_bytes(b"\0")
    (state / "control.json").write_text("{}", encoding="utf-8")

    state_store.delete_state_tree(state, preserve={".lock"})

    assert (state / ".lock").exists()
    assert not (state / "control.json").exists()


# ─── (b): archive через симлинк ─────────────────────────────────────────


def test_archive_refuses_symlinked_done_before_any_move(tmp_path):
    """Симлинк ``.agentic/done`` → отказ до move; исходники целы, факт не врёт."""
    from awf import publish, todos

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    done = project / ".agentic" / "done"
    _replace_with_symlink(done, outside)

    with pytest.raises(OSError):
        todos.archive_todo(project, T)

    # Ничего не перенесено наружу, источники на месте.
    assert not (outside / T).exists()
    assert (project / ".agentic" / "inbox" / f"{T}.md").is_file()
    assert (project / ".agentic" / "outbox" / f"DONE-{T}.md").is_file()
    # Факт архива не врёт: дерева архива нет.
    assert publish.publication_facts(project, T)["archived"] == ""


# ─── (c): lock-файл ─────────────────────────────────────────────────────


def test_lock_file_refuses_symlinked_leaf(tmp_path):
    """Симлинк на месте ``state/.lock`` → отказ, цель не тронута."""
    from awf import run_state
    from awf.infra.managed_fs import ManagedPathError

    project = _project(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("outside", encoding="utf-8")
    (project / ".agentic" / "state" / ".lock").symlink_to(victim)

    with pytest.raises(ManagedPathError):
        run_state._ensure_lock_file(project)

    assert victim.read_text(encoding="utf-8") == "outside"


# ─── (d): единый catch-лист «no state» ──────────────────────────────────


def test_catch_list_is_one_constant_with_three_classes():
    from awf._errors import LockUnavailable
    from awf.infra import state_store
    from awf.infra.managed_fs import ManagedPathError

    assert state_store.STATE_WRITE_FAILURES == (
        OSError,
        ManagedPathError,
        LockUnavailable,
    )
    definitions = [
        p
        for p in (REPO_ROOT / "awf").rglob("*.py")
        if "STATE_WRITE_FAILURES: tuple" in p.read_text(encoding="utf-8")
    ]
    assert [p.name for p in definitions] == ["__init__.py"], definitions


def test_writer_swallows_oserror(tmp_path, monkeypatch):
    from awf import pipeline_state
    from awf.infra import state_store

    project = _project(tmp_path)

    def boom(*_a, **_k):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(state_store, "write_state_file", boom)
    pipeline_state.write_state(
        project, logs_dir=project / ".agentic" / "logs", stage_name="x"
    )
    log = (project / ".agentic" / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    assert "T4.1" in log and "simulated disk failure" in log


def test_writer_swallows_managed_path_error(tmp_path):
    from awf import pipeline_state

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    _replace_with_symlink(project / ".agentic" / "state", outside)

    # Барьер сработал, но писатель деградирует до «no state», не падает.
    pipeline_state.write_state(
        project, logs_dir=project / ".agentic" / "logs", stage_name="x"
    )
    assert not (outside / "current.yaml").exists()
    assert not (project / ".agentic" / "state" / "current.yaml").exists()


def test_writer_swallows_lock_unavailable(tmp_path, monkeypatch):
    from awf import pipeline_state
    from awf._errors import LockUnavailable

    project = _project(tmp_path)

    def no_lock(_project_dir):
        raise LockUnavailable("no fcntl/msvcrt")

    monkeypatch.setattr("awf._lock.locked", no_lock)
    pipeline_state.write_state(project, stage_name="x")


def test_remover_crosses_the_boundary_as_oserror(tmp_path):
    """Уборщик ПРОПАГИРУЕТ отказ, но в документированной семье ``OSError``."""
    from awf import pipeline_state

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "current.yaml").write_text("stage_name: x\n", encoding="utf-8")
    _replace_with_symlink(project / ".agentic" / "state", outside)

    with pytest.raises(OSError):
        pipeline_state.clear_state(project)


# ─── (e): единая схема журнала операций ─────────────────────────────────


def _journal_schema():
    try:
        from awf.infra.state_store import _journal as schema
    except ImportError as exc:  # базовое дерево: модуля схемы ещё нет
        pytest.fail(f"journal schema module is missing: {exc}")
    return schema


def test_journal_status_literal_matches_the_one_vocabulary():
    """F3: ``OperationStatus`` — типизированное зеркало ``STATUSES``."""
    from typing import get_args

    schema = _journal_schema()
    assert set(get_args(schema.OperationStatus)) == set(schema.STATUSES)


def test_journal_both_writers_share_one_schema(tmp_path):
    from awf.api import hygiene
    from awf.infra import state_store
    from awf.infra.state_store._io import json_text

    schema = _journal_schema()
    project = _project(tmp_path)

    state_store.write_operation(
        project,
        "op-store",
        status="reserved",
        args_digest=state_store.payload_digest({"a": 1}),
    )
    hygiene._write_operation(
        project, "op-hyg", status="started", todo_id=T, action="update_todo"
    )

    for operation_id in ("op-store", "op-hyg"):
        raw = state_store.op_path(project, operation_id).read_text(encoding="utf-8")
        record = json.loads(raw)
        assert record["status"] in schema.STATUSES
        assert "args_digest" in record
        assert ISO_TS_RE.match(record["ts"]), record["ts"]
        assert raw.endswith("\n")
        # Байты зафиксированы: indent=2, отсортированные ключи.
        assert raw == json_text(record)
        assert '\n  "operation_id": ' in raw


def test_journal_dual_read_accepts_legacy_started(tmp_path):
    from awf.infra import state_store

    _journal_schema()
    project = _project(tmp_path)
    path = state_store.op_path(project, "op-legacy")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "operation_id": "op-legacy",
                "status": "started",
                "ts": "20261019T221213994647Z",
                "todo_id": T,
            }
        ),
        encoding="utf-8",
    )

    record = state_store.read_operation(project, "op-legacy")
    assert record["status"] == "running"


def test_journal_foreign_format_is_recovery_required(tmp_path):
    from awf._errors import OperationRecoveryRequired
    from awf.infra import state_store

    _journal_schema()
    project = _project(tmp_path)
    path = state_store.op_path(project, "op-foreign")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"operation_id": "op-foreign", "status": "exploded"}),
        encoding="utf-8",
    )

    with pytest.raises(OperationRecoveryRequired):
        state_store.read_operation(project, "op-foreign")


# ─── (f): pipeline_state создаёт state/ через барьер ────────────────────


def test_pipeline_state_does_not_create_through_symlink(tmp_path):
    """Висячий симлинк ``state`` → писатель деградирует, наружу не создаёт."""
    from awf import pipeline_state

    project = _project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    missing = outside / "missing"
    _replace_with_symlink(project / ".agentic" / "state", missing)

    pipeline_state.write_state(project, stage_name="x")

    assert not missing.exists()
