"""R2.1 (roadmap R2, unit R2.1): one untracked-path table, read by BOTH
readers.

F-QA4 closed the last branch where the model answered the same question two
ways: the untracked branch of ``changes()`` carried a path the MANIFEST
already excluded (a directory / nested repository), so
``operations_from_changes`` built an ownership operation over a directory git
cannot store as a content blob. The fix routes the change set through the
SAME resolution the manifest reads (``_untracked_selection``): secret-looking
names, directories / nested repositories, and paths that vanished mid-read
resolve to a ``reason`` in one place.

This file is the "one table instead of two branches" control the review
asked for. It runs one repository with every untracked path class through
both readers and asserts they agree class by class:

    class                 manifest                   change set
    --------------------  -------------------------  ---------------------
    plain file            selected (mode + blob)      record present
    executable file       selected mode 100755        record present
    symlink               selected mode 120000        record present
    secret-looking name   excluded "secret"           record ABSENT
    dir / nested repo     excluded "not representable" record ABSENT
    ignored file          excluded "ignored"          record ABSENT

The accepted-kind ownership consequence (a directory never becomes an owned
operation) is locked by the second test.

Green lock + regression: red on the pre-F-QA4 tree (directory was a change
record, then an ownership operation). All repositories are disposable in
``tmp_path``; real git is never touched.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

# (path, selected, exclusion_reason) — selected True means: present in the
# manifest's untracked list AND present in the change set.
CLASSES: list[tuple[bytes, bool, str | None]] = [
    (b"plain.txt", True, None),
    (b"script.sh", True, None),
    (b"link", True, None),
    (b".env", False, "secret"),
    (b"nested/", False, "directory/gitlink: not representable as content blob"),
    (b"build/artifact.bin", False, "ignored"),
]


def _model():
    """Ленивый импорт: на базовом дереве символа нет. Провал — утверждение
    «модуля нет», а не import-ошибка."""
    try:
        from awf.infra import git_model
    except ImportError as e:
        raise AssertionError(f"R2.1 git_model module is missing: {e}") from e
    return git_model


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _populate_classes(repo: Path) -> None:
    """Один репозиторий с одним путём каждого класса."""
    (repo / "plain.txt").write_bytes(b"p\n")
    (repo / "script.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(repo / "script.sh", 0o755)
    os.symlink("plain.txt", repo / "link")
    (repo / ".env").write_bytes(b"SECRET=1\n")
    (repo / "build").mkdir()
    (repo / "build" / "artifact.bin").write_bytes(b"x")
    nested = repo / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_bytes(b"i\n")


def _base_repo(tmp_path: Path):
    """Базовый репозиторий с коммитом правила ignore, затем все классы."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_bytes(b"build/\n")
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    _populate_classes(repo)
    return gm, repo


def test_untracked_path_class_table_is_consistent_between_readers(tmp_path):
    """Каждый класс untracked-пути манифест и набор изменений решают
    ОДИНАКОВО — одна таблица, не две ветки (F-QA4)."""
    gm, repo = _base_repo(tmp_path)

    manifest = gm.GitRepository(repo).capture_baseline()
    changes = gm.GitRepository(repo).changes()

    untracked = {u.path: u for u in manifest.untracked}
    excluded = {path: reason for path, reason in manifest.excluded}
    change_paths = {r.path for r in changes}

    for path, selected, reason in CLASSES:
        if selected:
            assert path in untracked, (
                f"{path!r} не выбран манифестом: untracked={list(untracked)!r}"
            )
            assert path in change_paths, (
                f"{path!r} отсутствует в наборе изменений: "
                f"{sorted(change_paths)!r}"
            )
        else:
            assert path not in untracked, (
                f"{path!r} не должен быть выбран: untracked={list(untracked)!r}"
            )
            assert excluded.get(path) == reason, (
                f"{path!r} исключён не с ожидаемой причиной: "
                f"excluded={manifest.excluded!r}"
            )
            assert path not in change_paths, (
                f"{path!r} просочился в набор изменений (читатели разошлись): "
                f"{sorted(change_paths)!r}"
            )

    # mode контракт двух выбранных не-plain классов не потерян рефактором
    assert untracked[b"script.sh"].mode == "100755"
    assert untracked[b"link"].mode == "120000"


def test_directory_after_baseline_is_never_an_owned_operation(tmp_path):
    """Каталог, появившийся ПОСЛЕ baseline, не становится собственной
    работой юнита — следствие единой таблицы (F-QA4)."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    baseline = gm.GitRepository(repo).capture_baseline()

    nested = repo / "vendor"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_bytes(b"i\n")

    ops = gm.operations_from_changes(gm.GitRepository(repo).changes())
    dir_ops = [op for op in ops if any(p.endswith(b"/") for p in op.paths)]
    assert not dir_ops, (
        f"каталог стал операцией владения: {[op.paths for op in ops]!r}"
    )

    result = gm.attribute_ownership(baseline, ops)
    owned_dirs = [
        op
        for op in result.owned
        if any(p.endswith(b"/") for p in op.paths)
    ]
    assert not owned_dirs, (
        f"каталог объявлен owned: {[op.paths for op in result.owned]!r}"
    )
