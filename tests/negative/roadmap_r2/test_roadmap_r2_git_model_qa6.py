"""R2.1 QA, iter 11 (roadmap R2, unit R2.1): `mode_change` reads the index.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, F-QA5).

``ChangeRecord.mode_change`` документирован как «pure mode flip (content
unchanged)» — чистый chmod 0644->0755. Но свойство сравнивает только
``base_mode`` (HEAD) и ``work_mode`` (рабочее дерево) и полностью
игнорирует ``index_mode`` — а именно индекс и есть то, что юнит
закоммитит. На ``modified``-записи эти факты расходятся:

* ``1 MM N... 100644 100755 100644`` — chmod +x ЗАСТЕЙДЖЕН, а в рабочем
  дереве режим возвращён. ``git diff --cached --summary`` печатает
  «mode change 100644 => 100755 f.txt»: смена режима есть в наборе
  изменений юнита. ``base_mode`` (100644) == ``work_mode`` (100644),
  поэтому ``mode_change`` возвращает ``False`` — модель ПРОПУСКАЕТ реальную
  staged-смену режима. R2.3 построит дельту на этом ``False`` и потеряет
  chmod (silent-класс, ради которого модуль и написан).

* ``1 M. N... 100644 100755 100755`` — chmod +x И правка содержимого,
  оба застейджены (``base_blob`` != ``index_blob``). Свойство возвращает
  ``True`` — «чистая смена режима» на записи, где содержимое изменилось.
  Обратная сторона той же ошибки: контентную правку можно принять за chmod.

Ожидаемое (из docstring-контракта): ``mode_change`` истинно, когда смена
режима есть в индексе ИЛИ в рабочем дереве (с учётом staged-режима), и
содержимое НЕ изменилось (``base_blob == index_blob``).

Красный-до: оба теста падают на текущем дереве (``False`` вместо
``True`` и ``True`` вместо ``False``). Прод-код стадией QA не правится
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


def test_staged_mode_change_is_not_hidden_by_a_worktree_revert(tmp_path):
    """Staged chmod +x, откатанный в рабочем дереве, остаётся сменой режима
    в наборе изменений — ``mode_change`` не должен терять ``index_mode``."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"v1\n")
    _commit(repo)

    os.chmod(repo / "f.txt", 0o755)
    _git(repo, "add", "f.txt")  # застейджили chmod 0644 -> 0755
    os.chmod(repo / "f.txt", 0o644)  # вернули режим в рабочем дереве

    view = gm.GitRepository(repo)
    rec = {r.path: r for r in view.changes()}[b"f.txt"]

    # независимое подтверждение: в индексе ДЕЙСТВИТЕЛЬНО лежит смена режима
    summary = _git(repo, "diff", "--cached", "--summary").stdout
    assert b"mode change 100644 => 100755" in summary, (
        f"предпосылка теста сломана, git не видит staged-смены режима: {summary!r}"
    )

    assert rec.index_mode != rec.base_mode, (
        f"index_mode={rec.index_mode!r} base_mode={rec.base_mode!r} — "
        "смена режима в индексе не отражена в записи"
    )
    assert rec.mode_change is True, (
        "staged-смена режима (index_mode "
        f"{rec.base_mode!r} -> {rec.index_mode!r}) объявлена отсутствующей: "
        f"mode_change={rec.mode_change!r} (base_mode={rec.base_mode!r}, "
        f"index_mode={rec.index_mode!r}, work_mode={rec.work_mode!r}); "
        "сравнение base vs work_mode теряет индекс — то, что юнит закоммитит"
    )


def test_staged_content_change_with_mode_flip_is_not_a_pure_mode_change(tmp_path):
    """chmod +x И правка содержимого, оба застейджены, — это НЕ «чистая
    смена режима»: ``mode_change`` не должен выдавать контентную правку за
    chmod."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"v1\n")
    _commit(repo)

    os.chmod(repo / "f.txt", 0o755)
    (repo / "f.txt").write_bytes(b"v2 different content\n")
    _git(repo, "add", "f.txt")

    view = gm.GitRepository(repo)
    rec = {r.path: r for r in view.changes()}[b"f.txt"]

    assert rec.base_blob != rec.index_blob, (
        f"предпосылка теста сломана: содержимое не изменилось "
        f"(base_blob={rec.base_blob!r}, index_blob={rec.index_blob!r})"
    )
    assert rec.mode_change is False, (
        "запись с ИЗМЕНЁННЫМ содержимым (base_blob "
        f"{rec.base_blob!r} -> index_blob {rec.index_blob!r}) объявлена "
        f"чистой сменой режима: mode_change={rec.mode_change!r}; "
        "docstring обещает «content unchanged»"
    )
