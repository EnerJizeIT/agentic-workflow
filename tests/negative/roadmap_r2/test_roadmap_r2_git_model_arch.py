"""R2.1 architect-review (roadmap R2, unit R2.1): failure typing, object
format, single-sourced dirty footprint and the copy/refusal-message
invariants.

Файл КРАСНЫЙ на дереве до фикса стадии архитект-ревью. Находки:

* A2 — ``GitRepository._run`` ловил только ``subprocess.TimeoutExpired``:
  соседние git-хелперы (``commit_gate``, ``commit_plan``) ловят
  ``(subprocess.SubprocessError, OSError)``. Отсутствующий git вылетал
  голым ``FileNotFoundError`` вместо ``GitModelError`` — контракт «явный
  отказ» рвался на том же классе, что закрыл M1.
* A3 — гвард пустого блоба сравнивал индексный блоб с зашитой sha1-константой
  ``EMPTY_BLOB``. В sha256-репозитории пустой блоб другой, гвард промахивался
  и пустой файл снова фабриковал copy-пару (тот же класс, что F1). Ожидаемое:
  пустой блоб берётся из самого репозитория (``git hash-object --stdin``).
* A1 — ``_dirty_paths`` сводил untracked дважды (свой ``changes()`` уже
  читает статус с ``--untracked-files=all``). Лишний источник убран; тест
  фиксирует, что untracked по-прежнему попадает в ``dirty`` из одного места.

* AR-4 (iter 5) — инвариант «любое сообщение отказа кодируется строгим
  UTF-8» (F-QA1) держался только на unmerged-сообщении. ``_run``
  склеивал argv как есть, а ``_stat_blob`` передаёт туда ПУТЬ в
  surrogateescape: на файле с байтом ``>= 0x80``, не складывающимся в
  UTF-8, сообщение об отказе снова несло lone surrogate и ломало тот же
  sink (``json.dumps(..., ensure_ascii=False)``). Инвариант обязан быть
  общим для всех отказов модуля, не «рядом» с одним сообщением.

Копия: ``operations_from_changes`` — единственное место, решающее
владение; copy НИКОГДА не становится парой (source — эвристика сходства).
Тест фиксирует это на уровне операции, а не только записи.

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import json
import os
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
        ["git", *args], cwd=repo, check=check, capture_output=True
    )


def _repo(tmp_path: Path, object_format: str | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    init = ["init", "-q"]
    if object_format:
        init += [f"--object-format={object_format}"]
    _git(repo, *init)
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit(repo: Path, message: str = "c") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def test_missing_git_is_a_typed_refusal(tmp_path, monkeypatch):
    """A2: недоступный git — ``GitModelError``, не голый ``FileNotFoundError``.

    ``_run`` обязан падать закрыто типизированной ошибкой на том же классе
    сбоев, что и соседние хелперы, а не только на ``TimeoutExpired`` (M1).
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    view = gm.GitRepository(repo)

    def _no_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(gm.subprocess, "run", _no_git)
    with pytest.raises(gm.GitModelError):
        view.head_sha()


def test_empty_file_stays_added_in_sha256_repo(tmp_path):
    """A3: гвард пустого блоба — из формата репозитория, не из sha1-константы.

    Два независимых пустых файла в sha256-репозитории совпадают пустым
    блобом; зашитая sha1-константа не совпала бы, и файл снова был бы
    помечен ``copied`` с чужим source. Ожидаемое: ``added``.
    """
    gm = _model()
    repo = _repo(tmp_path, object_format="sha256")
    probe = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=repo,
        input=b"",
        capture_output=True,
    )
    if probe.returncode != 0:
        pytest.skip("git does not support a sha256 object format")

    (repo / "a").mkdir()
    (repo / "a" / "__init__.py").write_bytes(b"")
    _commit(repo)
    (repo / "b").mkdir()
    (repo / "b" / "__init__.py").write_bytes(b"")
    _git(repo, "add", "b/__init__.py")

    records = {r.path: r for r in gm.GitRepository(repo).changes()}
    rec = records[b"b/__init__.py"]
    assert rec.kind == "added", (
        "в sha256-репозитории пустой файл не должен фабриковать copy-пару; "
        f"получено {rec.kind!r} (old_path={rec.old_path!r})"
    )
    assert rec.old_path is None, f"сфабрикованный source: {rec.old_path!r}"


def test_untracked_file_is_dirty_at_baseline(tmp_path):
    """A1: untracked приходит в ``dirty`` из одного источника (``changes()``).

    После снятия второго прохода ``untracked_paths`` набор обязан остаться
    тем же: untracked-файл — pre-existing факт baseline.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    (repo / "untracked.txt").write_bytes(b"u\n")

    manifest = gm.GitRepository(repo).capture_baseline()
    assert b"untracked.txt" in set(manifest.dirty)
    assert b"keep.txt" not in set(manifest.dirty)


def test_run_refusal_message_is_always_strict_utf8_encodable(tmp_path, monkeypatch):
    """AR-4: сообщение отказа ``_run`` с ПУТЁМ кодируется строгим UTF-8.

    ``_stat_blob`` передаёт путь в ``_run`` (``hash-object -- <path>``) в
    surrogateescape. Если на этом вызове git завис/недоступен, ``_run``
    строит сообщение склейкой argv; байт ``>= 0x80`` из имени уходит туда
    lone surrogate'ом и роняет тот же sink, что чинил F-QA1
    (``json.dumps(..., ensure_ascii=False)`` + строгая запись UTF-8).
    Ожидаемое: путь экранируется по байтовому значению, сообщение ВСЕГДА
    кодируется.
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "ok.txt").write_bytes(b"ok\n")
    _commit(repo)
    # untracked, so the fingerprint's untracked sweep passes it to
    # ``_run("hash-object", "--", <path>)`` — the path-bearing refusal
    bad = b"bad\xffname.txt"
    (repo / Path(os.fsdecode(bad))).write_bytes(b"payload\n")

    real_run = subprocess.run

    def _hang_on_hash(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if "hash-object" in cmd:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(gm.subprocess, "run", _hang_on_hash)

    with pytest.raises(gm.GitModelError) as exc:
        gm.GitRepository(repo).capture_baseline()
    message = str(exc.value)
    message.encode("utf-8")  # UnicodeEncodeError if a lone surrogate leaked
    json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    assert "\udcff" not in message, f"lone surrogate leaked: {message!r}"
    assert "\\xff" in message, f"path byte not escaped: {message!r}"


def test_copy_is_classified_but_never_an_ownership_pair(tmp_path):
    """Копия — информационная классификация, НЕ пара владения.

    ``changes()`` оставляет у копии ``old_path`` (source выбран эвристикой
    сходства), но ``operations_from_changes`` — единственное место, решающее
    владение, — обязан вернуть ОДИН путь (destination): чужой source не
    должен попадать в footprint операции (иначе — ложный conflict, F1).
    """
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    _git(repo, "config", "status.renames", "copies")
    (repo / "dst.txt").write_bytes(b"payload\n")
    _git(repo, "add", "dst.txt")

    view = gm.GitRepository(repo)
    record = {r.path: r for r in view.changes()}[b"dst.txt"]
    # informational classification keeps the heuristic source ...
    assert record.kind == "copied" and record.old_path == b"src.txt"

    # ... but ownership drops it: the destination stands alone
    dst_ops = [
        op
        for op in gm.operations_from_changes(view.changes())
        if b"dst.txt" in op.paths
    ]
    assert len(dst_ops) == 1
    assert dst_ops[0].old_path is None
    assert dst_ops[0].paths == (b"dst.txt",)
