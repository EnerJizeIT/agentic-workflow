"""R2.1 QA, iter 9 (roadmap R2, unit R2.1): untracked directory as an
ownership change.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

F-QA2 закрыл непредставимый untracked-каталог (вложенный репозиторий; git
листит его как ``dir/``) в МАНИФЕСТЕ: ``_classify_untracked`` пишет его в
``excluded`` с причиной «directory/gitlink: not representable as content
blob». Но тот же путь приходит в НАБОР ИЗМЕНЕНИЙ ``changes()`` обычной
untracked-записью (тег ``?``), а ``operations_from_changes`` делает из неё
операцию владения над каталогом.

Инвариант «непредставимое — явный отказ, не silent legacy» держится только
на ветке манифеста — ровно как до F-QA2 он держался только на indexed-ветке
``changes()``. Последствие конкретное: если каталог появился ПОСЛЕ baseline,
``attribute_ownership`` объявляет его СОБСТВЕННОЙ работой юнита (``owned``),
хотя git не хранит каталог как контент-блоб (при ``git add`` это gitlink
``160000``, который модель отвергает в indexed-ветке). R2.3 строит дельту на
операциях владения — каталог в ``owned`` сломает сборку кандидата.

Ожидаемое: ``changes()`` отвергает каталог явным ``GitModelError`` (как
gitlink в indexed-ветке) ЛИБО не отдаёт его операцией владения — но не
owned-путь каталога и не операция над каталогом.

Красный-до: тест падает на текущем дереве. Прод-код стадией QA не правится
(правило итерации); фикс — следующим проходом через REVIEW.

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


def _nested_repo(repo: Path, name: str) -> Path:
    """A nested git repository: git lists it as an untracked ``name/``
    directory it cannot represent as a content blob."""
    nested = repo / name
    nested.mkdir()
    _git(nested, "init", "-q")
    (nested / "inner.txt").write_bytes(b"i\n")
    return nested


def test_untracked_directory_is_not_an_ownership_operation(tmp_path):
    """Каталог не может быть операцией владения — как gitlink в indexed-ветке.

    ``changes()`` отдаёт вложенный репозиторий untracked-записью (путь со
    слэшем), и ``operations_from_changes`` делает над ним операцию. Каталог
    не представим контент-блобом, значит набор изменений не должен нести
    операций над ним: либо явный отказ ``changes()``, либо каталога в
    операциях нет.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    _nested_repo(repo, "nested")

    view = gm.GitRepository(repo)
    try:
        changes = view.changes()
    except gm.GitModelError:
        return  # явный отказ — собственный стиль модуля, приемлем

    ops = gm.operations_from_changes(changes)
    dir_ops = [
        op
        for op in ops
        if op.new_path.endswith(b"/")
        or (op.old_path is not None and op.old_path.endswith(b"/"))
    ]
    assert not dir_ops, (
        "каталог (вложенный репозиторий, путь со слэшем) стал операцией "
        "владения; git не хранит каталог как контент-блоб — нужен явный "
        f"отказ или отсутствие операции. operations={[op.paths for op in ops]!r}"
    )


def test_directory_appearing_after_baseline_is_not_owned(tmp_path):
    """Каталог, появившийся ПОСЛЕ baseline, не должен попасть в ``owned``.

    Живой сценарий R2: baseline снят на чистом дереве, затем юнит заводит
    вложенный репозиторий. Такой путь не dirty в baseline, поэтому
    ``attribute_ownership`` объявляет его собственным — хотя это каталог,
    который git не закоммитит как контент. R2.3 построит дельту на этом
    ``owned`` и сломается.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    baseline = gm.GitRepository(repo).capture_baseline()

    _nested_repo(repo, "vendor")

    view = gm.GitRepository(repo)
    try:
        ops = gm.operations_from_changes(view.changes())
    except gm.GitModelError:
        return  # явный отказ — приемлем
    result = gm.attribute_ownership(baseline, ops)

    owned_dirs = [
        op
        for op in result.owned
        if op.new_path.endswith(b"/")
        or (op.old_path is not None and op.old_path.endswith(b"/"))
    ]
    assert not owned_dirs, (
        "каталог объявлен СОБСТВЕННОЙ работой юнита (owned), хотя git не "
        "хранит каталог как контент-блоб: "
        f"owned={[op.paths for op in result.owned]!r}, "
        f"foreign={[op.paths for op in result.foreign]!r}"
    )
