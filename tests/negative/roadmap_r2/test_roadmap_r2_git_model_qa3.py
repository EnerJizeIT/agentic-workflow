"""R2.1 QA, iter 6 (roadmap R2, unit R2.1): untracked directory = silent legacy.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

``changes()`` отказывает на gitlink (mode ``160000``) — в индексе: подмодуль
не представим как контент R2.1. Но тот же gitlink ДО ``git add`` — обычная
untracked-запись: ``ls-files --others`` перечисляет вложенный репозиторий как
``nested/`` (со слэшем). ``_classify_untracked`` -> ``_stat_blob`` lstats этот
путь, видит каталог, и по маске ``st_mode & 0o100`` (у каталога owner-execute
всегда) записывает его как обычный ИСПОЛНЯЕМЫЙ файл: ``mode=100755``,
``blob=None``. Git не хранит каталог как контент-блоб и при ``git add`` сделал
бы gitlink — то есть модель молча фабрикует mode, которого git не присвоит
(тот же класс silent-legacy, что и отказ по gitlink, но на второй ветке
чтения; инвариант держится только там, где gitlink уже в индексе).

Ожидаемое: untracked-каталог — явный отказ (как gitlink в ``changes()``)
либо ``excluded`` с причиной; но не выбранный контент-файл с блобовым mode.

Красный-до: тест падает на текущем дереве. Прод-код стадией QA не правится
(правило итерации); фикс — следующим проходом через REVIEW.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


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


def test_untracked_nested_repo_directory_is_not_selected_as_executable_file(tmp_path):
    """Untracked-каталог (вложенный git-репозиторий) не должен попадать в
    ``untracked`` как исполняемый файл ``100755``.

    Git перечисляет вложенный репозиторий как untracked-путь ``nested/``;
    хранить каталог как контент он не может (при ``git add`` это gitlink,
    ``160000``, — что модель явно отвергает в ``changes()``). Значит, ветка
    untracked обязана вести себя так же: отказ или ``excluded`` с причиной,
    но никогда — выбранный контент-файл с блобовым mode.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    nested = repo / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_bytes(b"i\n")

    try:
        manifest = gm.GitRepository(repo).capture_baseline()
    except gm.GitModelError:
        return  # явный отказ — собственный стиль модуля, приемлем

    offenders = []
    for entry in manifest.untracked:
        full = repo / os.fsdecode(entry.path)
        if full.is_dir() and not full.is_symlink():
            if entry.mode in ("100644", "100755"):
                offenders.append((entry.path, entry.mode, entry.blob))
    assert not offenders, (
        "untracked-каталог выбран как контент-файл с блобовым mode: "
        f"{offenders!r} — git не хранит каталог как блоб; нужен отказ или "
        f"excluded с причиной. untracked={[(u.path, u.mode, u.blob) for u in manifest.untracked]!r}"
    )
