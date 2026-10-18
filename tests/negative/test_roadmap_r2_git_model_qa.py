"""R2.1 QA (roadmap R2, unit R2.1): copy-detection false pairs.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review).

``GitRepository.changes()`` безусловно переклассифицирует запись ``added``
в ``copied``, если ``git diff --cached --find-copies-harder`` нашёл пару.
Эвристика сходства даёт ложные пары на простом совпадении содержимого:
любые два пустых файла, или новый файл с содержимым, равным committed-blob
постороннего файла. Дальше ``operations_from_changes`` склеивает пару в
ОДНУ операцию владения, и если source-путь грязный в baseline, собственный
новый файл юнита эскалируется в conflict.

Красный-до: тесты падают на текущем дереве. Прод-код стадией QA не
правится (правило итерации) — фикс следующим проходом через REVIEW.

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


def test_new_file_coinciding_with_committed_blob_of_dirty_file_not_a_conflict(tmp_path):
    """Собственный новый файл юнита совпал содержимым с committed-blob
    постороннего грязного файла → ложный conflict.

    Юнит ничего не копировал у пользователя: его новый файл равен именно
    committed-версии ``a.txt``, а не текущей правке. Однако
    ``--find-copies-harder`` даёт пару, source грязный в baseline, и пара
    эскалируется в conflict — юнит не может закоммитить собственный файл.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"v1\n")
    _commit(repo)
    # чужая незакоммиченная правка (dirty в baseline), только worktree
    (repo / "a.txt").write_bytes(b"v2\n")
    baseline = gm.GitRepository(repo).capture_baseline()

    # новый файл юнита; содержимое равно committed-blob a.txt, не правке
    (repo / "new.txt").write_bytes(b"v1\n")
    _git(repo, "add", "new.txt")

    view = gm.GitRepository(repo)
    result = gm.attribute_ownership(
        baseline, gm.operations_from_changes(view.changes())
    )
    owned = {p for op in result.owned for p in op.paths}
    conflicts = {frozenset(op.paths) for op in result.conflicts}
    assert b"new.txt" in owned, (
        "собственный новый файл юнита не признан своим; "
        f"owned={[op.paths for op in result.owned]}, "
        f"foreign={[op.paths for op in result.foreign]}, "
        f"conflicts={[op.paths for op in result.conflicts]}"
    )
    assert frozenset({b"a.txt", b"new.txt"}) not in conflicts


def test_fresh_empty_file_is_not_a_fabricated_copy_of_another_empty_file(tmp_path):
    """Свежий пустой файл (типовой новый ``__init__.py``) помечен как
    ``copied`` с чужим source-путём — пары, которой юнит не делал, не было.

    Два независимых пустых файла совпадают содержимым; эвристика
    ``--find-copies-harder`` считает это копией. ``kind`` и ``old_path``
    фабрикуются: модель утверждает перенос, которого не было, и тянет
    посторонний путь в footprint операции.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a").mkdir()
    (repo / "a" / "__init__.py").write_bytes(b"")
    _commit(repo)
    (repo / "b").mkdir()
    (repo / "b" / "__init__.py").write_bytes(b"")
    _git(repo, "add", "b/__init__.py")

    records = {r.path: r for r in gm.GitRepository(repo).changes()}
    rec = records[b"b/__init__.py"]
    assert rec.kind == "added", (
        f"свежий файл должен остаться added, получено {rec.kind!r} "
        f"(old_path={rec.old_path!r})"
    )
    assert rec.old_path is None, f"сфабрикованный source: {rec.old_path!r}"


def test_copy_source_is_not_a_baseline_fact(tmp_path):
    """Эвристическая copy-пара не делает source частью baseline.

    Тот же класс, что F1, но на другом потребителе ``old_path``:
    ``GitRepository._dirty_paths()`` раньше брал footprint из сырых записей
    и добавлял ``old_path`` любой пары, включая сфабрикованный copy-source.
    Незатронутый committed-файл попадал в ``dirty`` только потому, что его
    committed-blob совпал с новым staged-файлом, — и дальше юнит не мог
    закоммитить собственную правку этого файла.

    Теперь footprint берётся из ``operations_from_changes`` (одно
    определение владения), поэтому similarity-guessed source в ``dirty``
    не протекает.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    # чужая pre-existing работа: staged-файл, равный committed-blob src.txt
    (repo / "dst.txt").write_bytes(b"payload\n")
    _git(repo, "add", "dst.txt")

    baseline = gm.GitRepository(repo).capture_baseline()
    dirty = set(baseline.dirty)
    assert b"dst.txt" in dirty, "staged-файл действительно dirty"
    assert b"src.txt" not in dirty, (
        "незатронутый committed-файл признан dirty из-за copy-эвристики; "
        f"dirty={sorted(dirty)}"
    )
