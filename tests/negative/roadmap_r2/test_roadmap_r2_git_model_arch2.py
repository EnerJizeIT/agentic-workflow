"""R2.1 architect-review, iter 7 (roadmap R2, unit R2.1): the guard's own
failure mode, and the single builder for porcelain-v2 records.

Файл КРАСНЫЙ на дереве до фикса стадии архитект-ревью (AR-6).

* AR-6 — ``changes()`` защищает от «фабрикованной пары» сравнением
  индексного блоба записи с пустым блобом репозитория
  (``r.index_blob != empty``). Пустой блоб берётся из самого репозитория
  (``_empty_blob``, ``git hash-object --stdin``), и его docstring обещает:
  «``None`` when the call fails — the empty-file guard then matches
  nothing». На деле при ``empty is None`` условие ``r.index_blob != None``
  истинно для ЛЮБОЙ записи, и гвард молча выключается: свежий пустой файл
  снова получает ``kind="copied"`` и чужой source в ``old_path`` —
  ровно класс F1, ради которого гвард существует. Отказ гит-вызова
  (hash-object недоступен) не должен ОСЛАБЛЯТЬ инвариант: гвард падает
  ЗАКРЫТО (не переклассифицирует), а не открыто.

* AR-5 (замок, не красный) — ``_parse_status_v2`` собирал ``ChangeRecord``
  двумя почти одинаковыми ветками (формат ``1`` и формат ``2``) с одним и
  тем же набором mode/blob-фактов. Сведены в один билдер: факты форматов
  нельзя рассинхронизировать (фикс в одной ветке не мог не попасть в
  другую). Тест фиксирует, что запись rename несёт тот же полный набор
  mode/blob-фактов, что и обычная modified-запись формата ``1``.

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


def test_copy_guard_fails_closed_when_empty_oid_unknown(tmp_path, monkeypatch):
    """AR-6: при неизвестном пустом блобе гвард НЕ переклассифицирует.

    ``_empty_blob`` вернул ``None`` (git не смог отдать пустой oid) — гвард
    обязан уйти в безопасную сторону: свежий пустой файл остаётся
    ``added`` без пары, а не фабрикует ``copied`` на чужой committed пустой
    файл (класс F1). Docstring ``_empty_blob`` уже обещает именно это.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "other.txt").write_bytes(b"")  # чужой committed пустой файл
    _commit(repo)
    _git(repo, "config", "status.renames", "copies")
    (repo / "fresh.txt").write_bytes(b"")  # свежий пустой файл юнита
    _git(repo, "add", "fresh.txt")

    view = gm.GitRepository(repo)
    # базовая линия: при известном пустом блобе гвард работает — added
    assert {r.path: r for r in view.changes()}[b"fresh.txt"].kind == "added"

    monkeypatch.setattr(gm.GitRepository, "_empty_blob", lambda self: None)
    rec = {r.path: r for r in view.changes()}[b"fresh.txt"]
    assert rec.kind == "added", (
        "известный пустой блоб потерян — гвард обязан падать ЗАКРЫТО, "
        f"а не фабриковать пару; получено {rec.kind!r} (old_path={rec.old_path!r})"
    )
    assert rec.old_path is None, f"сфабрикованный source: {rec.old_path!r}"


def test_rename_record_carries_full_mode_blob_facts(tmp_path):
    """AR-5 (замок): rename-запись (формат ``2``) несёт тот же набор
    mode/blob-фактов, что и modified-запись (формат ``1``).

    Оба формата собираются ОДНИМ билдером: запись не должна быть «тоньше»
    в зависимости от формата, иначе рассинхрон двух бывших веток проходит
    незамеченным.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"keep\n")
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    (repo / "keep.txt").write_bytes(b"changed\n")
    _git(repo, "mv", "src.txt", "dst.txt")

    records = {r.path: r for r in gm.GitRepository(repo).changes()}
    modified = records[b"keep.txt"]
    renamed = records[b"dst.txt"]
    assert modified.kind == "modified"
    assert renamed.kind == "renamed" and renamed.old_path == b"src.txt"
    for rec in (modified, renamed):
        assert rec.base_mode and rec.index_mode and rec.work_mode, (
            f"mode-факты неполны у {rec.path!r}: {rec!r}"
        )
        assert rec.base_blob and rec.index_blob, (
            f"blob-факты неполны у {rec.path!r}: {rec!r}"
        )
