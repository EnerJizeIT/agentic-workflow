"""CARRY IMP3: POSIX-барьер архивации (carry-cd R2, AC4–AC5).

Закрытый класс (C2): ``archive_todo`` проверял no-follow ДО перемещения, но
на платформе без lstat/O_NOFOLLOW этой гарантии не существует — архив либо
падал невнятно, либо (при будущей слабой реализации) молча работал.
Инвариант: не-POSIX отказывает ПЕРВЫМ действием, тем же ``OSError``, и ни
один файл не перемещён и не удалён.

Тесты не-архивные поверхности (inbox → симлинк) держат тот же барьер: отказ
до эффекта, внешняя канарейка цела.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from awf import todos
from awf.infra import managed_fs

T = "TODO-0001"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "done", "handoff"):
        (project / ".agentic" / name).mkdir(parents=True)
    inbox = project / ".agentic" / "inbox"
    (inbox / f"{T}.md").write_text("# task\n", encoding="utf-8")
    (inbox / f"{T}.ready").write_text("", encoding="utf-8")
    outbox = project / ".agentic" / "outbox"
    (outbox / f"DONE-{T}.md").write_text("done\n", encoding="utf-8")
    (outbox / f"DONE-{T}.json").write_text("{}\n", encoding="utf-8")
    (outbox / f"DONE-{T}.ready").write_text("", encoding="utf-8")
    return project


def _snapshot(project: Path) -> dict[str, bytes]:
    root = project / ".agentic"
    return {
        p.relative_to(project).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _outside(tmp_path: Path) -> tuple[Path, Path]:
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    return outside, canary


def test_archive_refuses_non_posix_before_any_move(tmp_path, monkeypatch):
    """AC4: не-POSIX — отказ до перемещения; дерево и канарейка те же."""
    project = _project(tmp_path)
    before = _snapshot(project)
    monkeypatch.setattr(managed_fs, "is_posix", lambda: False)

    with pytest.raises(OSError, match="POSIX") as exc:
        todos.archive_todo(project, T)

    assert f"archive {T}" in str(exc.value)
    assert _snapshot(project) == before, "a refused archive must not move or delete anything"
    assert not (project / ".agentic" / "done" / T).exists()


def test_archive_transfers_on_posix(tmp_path):
    """AC5: на POSIX переносятся те же файлы, сигналы потреблены."""
    project = _project(tmp_path)

    dest = todos.archive_todo(project, T)

    assert dest is not None
    assert dest == project / ".agentic" / "done" / T
    assert (dest / "TODO.md").read_text(encoding="utf-8") == "# task\n"
    assert (dest / "DONE.md").read_text(encoding="utf-8") == "done\n"
    assert (dest / "DONE.json").read_text(encoding="utf-8") == "{}\n"
    inbox = project / ".agentic" / "inbox"
    outbox = project / ".agentic" / "outbox"
    assert not (inbox / f"{T}.md").exists()
    assert not (inbox / f"{T}.ready").exists()
    assert not (outbox / f"DONE-{T}.ready").exists()


def test_archive_refuses_symlinked_inbox_before_effect(tmp_path):
    """AC5: симлинк поверхности по-прежнему отказывает до эффекта."""
    project = _project(tmp_path)
    outside, canary = _outside(tmp_path)
    (outside / f"{T}.md").write_text("# foreign\n", encoding="utf-8")
    inbox = project / ".agentic" / "inbox"
    (inbox / f"{T}.md").unlink()
    (inbox / f"{T}.ready").unlink()
    inbox.rmdir()
    inbox.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink") as exc:
        todos.archive_todo(project, T)

    assert f"archive {T}" in str(exc.value)
    assert canary.read_text(encoding="utf-8") == "keep me"
    assert (outside / f"{T}.md").read_text(encoding="utf-8") == "# foreign\n"
    assert not (project / ".agentic" / "done" / T).exists()
