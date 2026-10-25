"""R2.1 audit, iter 4 (roadmap R2, unit R2.1): the stability gate itself.

Файл КРАСНЫЙ на дереве до фикса стадии аудитора (iter 4). Четыре находки:

* H1 — ``tree_fingerprint`` делегировал в строковый
  ``git_utils.tree_fingerprint``: untracked-имена читались через
  ``ls-files`` + ``splitlines()``, поэтому перевод строки в имени рвал
  список — файл не попадал в контентный хеш, и смена его содержимого не
  меняла отпечаток. Дерево могло быть объявлено «стабильным», двигаясь.
* M1 — ``_run`` не ловил ``subprocess.TimeoutExpired``: зависший git
  вылетал голым исключением, а не типом ``GitModelError``.
* M2 — sparse/split ловил только ``preflight()``: прямой ``changes()`` на
  sparse-дереве молча читал проекцию (неполное дерево).
* M3 — ``_SECRET_NAMES`` содержал голое ``credentials``: любой untracked с
  таким именем исключался как секрет, независимо от содержимого.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


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
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
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


def test_fingerprint_sees_newline_named_untracked_file_change(tmp_path):
    """H1: отпечаток читает untracked через NUL-протокол, не ``splitlines()``.

    Имя с переводом строки не должно «выпадать» из контентного хеша: смена
    содержимого такого файла обязана менять отпечаток — иначе страж
    стабильности объявит двинувшееся дерево стабильным.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)

    weird = repo / "line\nname.txt"
    weird.write_bytes(b"v1\n")
    first = gm.tree_fingerprint(repo)

    weird.write_bytes(b"v2 changed\n")
    second = gm.tree_fingerprint(repo)

    assert first != second, (
        "смена содержимого untracked-файла с переводом строки в имени "
        "обязана менять отпечаток (splitlines() его терял)"
    )


def test_fingerprint_stable_when_nothing_moves(tmp_path):
    """H1: тот же дерево дважды — тот же отпечаток (нет ложного дрейфа)."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    (repo / "untracked.txt").write_bytes(b"u\n")

    assert gm.tree_fingerprint(repo) == gm.tree_fingerprint(repo)


def test_git_timeout_is_a_typed_refusal(tmp_path, monkeypatch):
    """M1: зависший git — ``GitModelError``, не голый ``TimeoutExpired``.

    Соседние git-хелперы падают закрыто типизированной ошибкой; ``_run``
    обязан делать то же, иначе контракт «явный отказ» ломается."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    view = gm.GitRepository(repo)

    def _boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=gm.GIT_TIMEOUT)

    monkeypatch.setattr(gm.subprocess, "run", _boom)
    with pytest.raises(gm.GitModelError):
        view.head_sha()


def test_changes_refuses_sparse_checkout_without_preflight(tmp_path):
    """M2: ``changes()`` сам отказывает на sparse, не только ``preflight``.

    Прямой read-API на проекции дерева иначе молча вернул бы неполный
    набор изменений."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    _git(repo, "config", "core.sparseCheckout", "true")
    (repo / "a.txt").write_bytes(b"changed\n")

    with pytest.raises(gm.UnsupportedSparseCheckoutError):
        gm.GitRepository(repo).changes()


def test_bare_credentials_file_is_not_a_secret(tmp_path):
    """M3: голое имя ``credentials`` — не секрет; известный путь — да.

    ``_SECRET_NAMES`` с голым ``credentials`` исключал любой untracked с
    таким именем; сужено до известных путей-хранилищ (``.aws/credentials``).
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    (repo / "credentials").write_bytes(b"# a fixture, not a secret\n")
    (repo / ".aws").mkdir()
    (repo / ".aws" / "credentials").write_bytes(b"[default]\n")

    manifest = gm.GitRepository(repo).capture_baseline()
    untracked = {u.path for u in manifest.untracked}
    excluded = {path: reason for path, reason in manifest.excluded}

    assert b"credentials" in untracked, (
        "голое имя credentials не должно исключаться как секрет "
        f"(excluded={manifest.excluded!r})"
    )
    assert excluded.get(b".aws/credentials") == "secret", (
        "известный путь-хранилище обязан остаться исключённым как секрет: "
        f"{manifest.excluded!r}"
    )
