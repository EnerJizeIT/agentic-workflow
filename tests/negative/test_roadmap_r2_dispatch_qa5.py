"""R2.2 QA (TODO-0221, iter 5): the R2.1 raw rework broke the leak-gate's
"never raises" contract on a TRANSIENT git failure.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, iter 5).

Итерация 5 перевела ``reject_files`` на raw-модель R2.1: ``_raw_untracked``
читает пути через ``GitRepository.untracked_paths()`` (``-z``, байты), а
``_untracked_view`` ДОБАВЛЯЕТ второй git-вызов — печатный список
``git_utils.untracked_files`` — чтобы позиционно связать raw-путь с его
печатной формой (мост к ``BASELINE-*.untracked``).

До итерации 5 вся git-работа ``reject_files`` шла ОДНИМ вызовом, обёрнутым в
``except (subprocess.SubprocessError, OSError)`` → ``None`` (best-effort).
Теперь guard стоит только на первом вызове (``_raw_untracked`` ловит
``GitModelError``/``OSError``); второй вызов — ``git_utils.untracked_files`` —
НЕ обёрнут, а ``git_utils._git`` пропускает ``subprocess.TimeoutExpired`` и
``OSError`` наружу. Поэтому зависший/пропавший git на втором чтении валит:

* ``snapshot_rejected_files`` — контракт «Never raises — the reject must not
  fail on it» (RUN5 #1 Part A.1), т.е. падает сам ``awf reject``;
* ``orphaned_reject_files`` — контракт «[] when … git cannot measure»
  (failsafe-поверхность verify-pack / approve).

Класс ровно тот, что итерация 5 закрыла для ``UnicodeEncodeError`` (arch
AR-1), но по другому триггеру: не кодировка имени, а сбой git. Обе ветки
обязаны деградировать в ``[]``/``None``, а не вылетать.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, git_utils, paths, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"
FINDING = "finding.txt"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed disposable repo with an awf skeleton."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "tester")
    api.init_project(
        r, project_name="R2QA5", test_cmd="", lint_cmd="",
        typecheck_cmd="", build_cmd="",
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "awf init")
    return r


@pytest.fixture
def second_read_fails(monkeypatch):
    """Only the PRINTED listing (``git_utils.untracked_files`` — the new
    second read of ``_untracked_view``) fails; the raw model read stays live,
    so the failure lands exactly on the unguarded call."""

    def boom(_project_dir):
        raise subprocess.TimeoutExpired(cmd="git ls-files", timeout=30)

    monkeypatch.setattr(git_utils, "untracked_files", boom)


def test_snapshot_never_raises_on_transient_git_failure(repo, second_read_fails):
    """A hung git on the printed read must not fail the reject: the snapshot is
    best-effort and degrades to ``[]`` (RUN5 #1 Part A.1)."""
    (repo / FINDING).write_text("finding\n", encoding="utf-8")
    recorded = reject_files.snapshot_rejected_files(
        repo, SRC, paths.logs_dir(repo)
    )
    assert recorded == [], (
        "snapshot_rejected_files must swallow a git failure on the printed "
        f"read and return []; got {recorded!r}"
    )


def test_orphaned_never_raises_on_transient_git_failure(repo, second_read_fails):
    """The failsafe must return ``[]`` when git cannot measure, not raise."""
    (repo / FINDING).write_text("finding\n", encoding="utf-8")
    ctx = paths.context_dir(repo)
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"REJECT-{SRC}.files").write_text(FINDING + "\n", encoding="utf-8")
    (ctx / f"BASELINE-{RETRY}.untracked").write_text(FINDING + "\n", encoding="utf-8")

    result = reject_files.orphaned_reject_files(repo, RETRY)
    assert result == [], (
        "orphaned_reject_files must return [] when git cannot measure; "
        f"got {result!r}"
    )


def test_snapshot_never_raises_with_quotepath_false_non_utf8(repo, monkeypatch):
    """A DETERMINISTIC trigger, no failure injection: with ``core.quotepath=false``
    (a legitimate user config) ``git ls-files`` emits the raw non-UTF-8 bytes,
    which ``git_utils._git(text=True)`` decodes strictly → ``UnicodeDecodeError``.
    The second read of ``_untracked_view`` is unguarded, so the reject fails."""
    import os

    _git(repo, "config", "core.quotepath", "false")
    name = b"\xff\xfe.txt"
    fd = os.open(os.fsencode(repo) + b"/" + name, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"finding\n")
    os.close(fd)

    recorded = reject_files.snapshot_rejected_files(
        repo, SRC, paths.logs_dir(repo)
    )
    assert recorded == [], (
        "snapshot_rejected_files must degrade to [] (best-effort) instead of "
        "raising UnicodeDecodeError on a non-UTF-8 name under "
        f"core.quotepath=false; got {recorded!r}"
    )
