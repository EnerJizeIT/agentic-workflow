"""R2.1 QA, iter 8 (roadmap R2, unit R2.1): intent-to-add = silent misclassification.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

``git add -N`` (intent-to-add — его ставят ``git add -p``/``-i`` и часть
редакторов) заводит НОВЫЙ путь в индексе намерением. В машинном протоколе
``git status --porcelain=v2 -z`` такая запись выглядит так::

    1 .A N... 000000 000000 100644 <40 нулей> <40 нулей> new.txt

X = ``.`` (в индексе нет содержимого), Y = ``A`` (рабочее дерево добавило
путь). ``git status --short`` для того же файла печатает `` A new.txt`` —
это НОВЫЙ путь, а не правка существующего.

``GitRepository._kind`` (код ``git_model.py:798-812``) разбирает только
``x == "A"``; ветки ``y == "A"`` нет, поэтому запись проваливается в
последний ``return "modified"``. Следствия на текущем дереве:

* ``kind`` нового файла = ``"modified"`` — модель утверждает правку пути,
  которого нет в HEAD (``base_blob`` = 40 нулей);
* ``mode_change`` = ``True``: ``base_mode "000000"`` != ``work_mode
  "100644"`` при ``kind == "modified"``. Новый файл репортится как ЧИСТАЯ
  смена режима (класс chmod 0644→0755), хотя содержимого не было вовсе.

Это тот же класс, что закрывали F1/F-QA2/M1: неподдержанное/особое
состояние проходит молча, вместо корректной классификации или явного
отказа. Требование 1 (``kind``) и общая планка приёмки «Модель
классифицирует корректно; на неподдержанном — явный отказ, не silent
legacy» нарушены: ``git add -N`` — это добавление, а не модификация.

Ожидаемое: ``kind`` в (``"added"``, ``"untracked"``) либо явный
``GitModelError`` из ``changes()``; НЕ ``"modified"`` и НЕ
``mode_change == True``.

Красный-до: тест падает на текущем дереве (``kind == "modified"``,
``mode_change is True``). Прод-код стадией QA не правится (правило
итерации); фикс — следующим проходом через REVIEW.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

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


def test_intent_to_add_file_is_not_silently_a_modification(tmp_path):
    """``git add -N`` заводит НОВЫЙ путь; модель не должна выдавать его за
    правку существующего файла (``kind="modified"``) и тем более за чистую
    смену режима (``mode_change == True``)."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "b.txt").write_bytes(b"b\n")
    _commit(repo)

    (repo / "new.txt").write_bytes(b"brand new content\n")
    _git(repo, "add", "-N", "new.txt")  # intent-to-add

    view = gm.GitRepository(repo)
    try:
        records = {r.path: r for r in view.changes()}
    except gm.GitModelError:
        return  # явный отказ — собственный стиль модуля, приемлем

    rec = records.get(b"new.txt")
    assert rec is not None, (
        "intent-to-add-файл исчез из change set: "
        f"{[(r.path, r.kind) for r in view.changes()]!r}"
    )
    assert rec.kind in ("added", "untracked"), (
        "intent-to-add заводит НОВЫЙ путь (git status --short: ' A new.txt') — "
        f"получено kind={rec.kind!r} (base_blob={rec.base_blob!r}); "
        "«modified» изобретает базовую версию, которой в HEAD нет"
    )
    assert rec.mode_change is False, (
        "новый файл репортится как чистая смена режима "
        f"(base_mode={rec.base_mode!r} -> work_mode={rec.work_mode!r}); "
        "у intent-to-add нет содержимого в индексе, это не chmod"
    )
