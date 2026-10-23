"""R2.1 audit (roadmap R2, unit R2.1): silent degradation and non-bytes.

Файл КРАСНЫЙ на дереве до фикса стадии аудитора (iter 3). Четыре находки:

* M1 — ``changes()`` читал статус без явного ``--find-renames``: короткий
  конфиг ``status.renames=false`` молча разбирал staged rename на delete +
  add, пара владения не собиралась (деградация требования 4).
* M2 — путь, исчезнувший между листингом untracked и ``lstat``, молча
  выбирался с угаданным mode ``100644`` вместо ``excluded`` с причиной
  (silent-legacy, требование 2).
* M3 — executable-детект ``st_mode & 0o111`` шире правила git
  (``S_IXUSR`` = ``0o100``): ``0611`` записывался как ``100755``, git
  записал бы ``100644``.
* M4 — блоб симлинка считался самодельным sha1-заголовком: неверно для
  ``extensions.objectFormat=sha256``. Замена — ``git hash-object --stdin``
  (цель ссылки в stdin), корректно для обоих форматов; на обычном
  (sha1) репо digest совпадает с ``git hash-object --stdin``.

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


def _git(
    repo: Path, *args: str, check: bool = True, input: bytes | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        input=input,
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


def test_staged_rename_survives_status_renames_false_config(tmp_path):
    """M1: ``changes()`` обязан звать статус с явным ``--find-renames``.

    Короткий конфиг ``status.renames=false`` иначе молча разберёт staged
    rename на delete+add: пара владения не соберётся — деградация
    требования 4 без единого отказа.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    _git(repo, "config", "status.renames", "false")
    _git(repo, "mv", "src.txt", "dst.txt")

    view = gm.GitRepository(repo)
    by_path = {r.path: r for r in view.changes()}
    assert b"dst.txt" in by_path
    assert by_path[b"dst.txt"].kind == "renamed", (
        "status.renames=false молча разобрал пару: "
        f"{[(r.path, r.kind) for r in view.changes()]}"
    )
    assert by_path[b"dst.txt"].old_path == b"src.txt"
    assert b"src.txt" not in by_path


def test_untracked_vanishing_between_listing_and_stat_is_recorded_excluded(
    tmp_path, monkeypatch
):
    """M2: путь исчез между листингом untracked и stat — явный excluded с
    причиной, а не молчаливый None (класс silent-legacy, требование 2)."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    (repo / "real.py").write_bytes(b"x = 1\n")
    view = gm.GitRepository(repo)
    listed = view.untracked_paths

    def _listing():
        return [*listed(), b"ghost.py"]

    monkeypatch.setattr(view, "untracked_paths", _listing)
    manifest = view.capture_baseline()

    excluded = {path: reason for path, reason in manifest.excluded}
    untracked = {u.path for u in manifest.untracked}
    assert b"ghost.py" not in untracked, "исчезнувший путь не должен быть выбран"
    assert b"ghost.py" in excluded, (
        f"исчезнувший путь не записан excluded с причиной: {manifest.excluded!r}"
    )
    assert excluded[b"ghost.py"] == "vanished during capture"
    assert b"real.py" in untracked


def test_untracked_executable_bit_follows_git_owner_x_rule(tmp_path):
    """M3: executable-детект — owner-execute (0100), как у git.

    ``0611`` (group/other x, без owner x) git записывает как ``100644``;
    широкая маска ``0o111`` ложно давала ``100755``.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "base.txt").write_bytes(b"b\n")
    _commit(repo)
    odd = repo / "odd.sh"
    odd.write_bytes(b"#!/bin/sh\n")
    os.chmod(odd, 0o611)
    run = repo / "run.sh"
    run.write_bytes(b"#!/bin/sh\n")
    os.chmod(run, 0o755)

    modes = {
        u.path: u.mode for u in gm.GitRepository(repo).capture_baseline().untracked
    }
    assert modes[b"odd.sh"] == "100644", (
        "git пишет 100755 только при owner-execute (0100); "
        f"получено {modes.get(b'odd.sh')!r}"
    )
    assert modes[b"run.sh"] == "100755"


def test_symlink_blob_matches_git_hash_object(tmp_path):
    """M4: блоб симлинка — через ``git hash-object --stdin`` (цель ссылки в
    stdin), не самодельный sha1-заголовок.

    Самодельный sha1 неверен для ``extensions.objectFormat=sha256``; при
    этом ссылку читать нельзя — только строку цели.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "target.txt").write_bytes(b"target\n")
    _commit(repo)
    os.symlink("target.txt", repo / "link.txt")

    entries = {
        u.path: u for u in gm.GitRepository(repo).capture_baseline().untracked
    }
    assert b"link.txt" in entries
    assert entries[b"link.txt"].mode == "120000"
    expected = (
        _git(repo, "hash-object", "--stdin", input=b"target.txt")
        .stdout.decode("ascii")
        .strip()
    )
    assert entries[b"link.txt"].blob == expected, (
        "блоб симлинка должен совпадать с git hash-object, "
        "а не самодельным sha1-заголовком"
    )
    target_blob = (
        _git(repo, "hash-object", "--", "target.txt").stdout.decode("ascii").strip()
    )
    assert entries[b"link.txt"].blob != target_blob, (
        "ссылка не читается: блоб — строка цели, не байты файла за ссылкой"
    )
