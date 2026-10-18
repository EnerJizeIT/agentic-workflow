"""R2.1 architect-review, iter 8 (roadmap R2, unit R2.1): one resolver for
untracked paths.

Файл ЗЕЛЁНЫЙ до и после фикса — это ЗАМОК (как AR-5 в arch2), а не
красный: рефакторинг F-arch поведение не меняет, тест фиксирует, что
единый резолвер сохранил детерминизм отпечатка на непредставимом
untracked-пути.

* F-arch — перевод двух внутренних исключений
  (``_UntrackedVanished`` / ``_UntrackedNotRepresentable``) в факт был
  написан ДВАЖДЫ: в ``_classify_untracked`` (в ``excluded`` с причиной) и
  в ``tree_fingerprint`` (в маркер хеша). Два потребителя одного правила
  легко расходятся: новое неподдержанное состояние untracked можно
  обработать в одном и молча проглотить в другом. Сведено в один
  ``_resolve_untracked``: манифест и страж стабильности читают одну
  резолюцию. Тест фиксирует, что страж стабильности продолжает видеть
  непредставимый untracked-путь (каталог/вложенный репозиторий) как
  ДЕТЕРМИНИРОВАННЫЙ факт: два чтения одного дерева равны, а смена соседа
  меняет отпечаток.

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


def test_fingerprint_is_deterministic_with_unrepresentable_untracked(tmp_path):
    """F-arch: непредставимый untracked-путь (вложенный репозиторий) — часть
    отпечатка, а не «немой» пропуск.

    До фикса маркер этого состояния был «написан рядом» в
    ``tree_fingerprint`` отдельно от ``_classify_untracked``. Единый
    резолвер обязан оставить страж стабильности детерминированным: два
    чтения одного дерева равны, смена соседнего untracked-файла — меняет
    отпечаток.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)

    nested = repo / "nested"
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_bytes(b"i\n")
    (repo / "neighbour.txt").write_bytes(b"v1\n")

    first = gm.tree_fingerprint(repo)
    assert first == gm.tree_fingerprint(repo), (
        "отпечаток нестабилен при непредставимом untracked-пути (вложенный "
        "репозиторий): единый резолвер обязан давать детерминированный факт"
    )

    (repo / "neighbour.txt").write_bytes(b"v2 changed\n")
    assert gm.tree_fingerprint(repo) != first, (
        "смена соседнего untracked-файла не изменила отпечаток — страж "
        "стабильности потерял untracked-контент"
    )
