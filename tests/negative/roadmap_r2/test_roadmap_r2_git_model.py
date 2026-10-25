"""R2.1 (roadmap R2): git-model without printed lines.

На исходной версии файл КРАСНЫЙ: модуля ``awf.infra.git_model`` нет;
импорт ленивый, поэтому падают утверждения, а не сбор. Строки приёмки
R2.1: пути — реальные байты (кириллица, пробелы, кавычки, tab, ведущий
dash), не strip/C-decode; NUL-протокол без разбора печатных строк;
модель классифицирует modechange/binary/typechange/delete+add/copy/
пустой/CRLF; неподдержанное (unmerged/sparse/split) — явный отказ до
побочных эффектов; BaselineManifest immutable и versioned, захват
проверяет стабильность дерева; rename — одна операция из пары путей,
принадлежность берётся из baseline (S2-01).

Все репозитории — disposable в ``tmp_path``. Реальный git не трогается.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _model():
    """Ленивый импорт: на базовом дереве символа нет. Провал — утверждение
    «модуля нет», а не import-ошибка: так baseline даёт настоящий красный,
    а не broken-runner."""
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


# ─── paths: raw bytes, no strip / C-decode ───────────────────────────────


def test_paths_are_raw_bytes_not_stripped_or_cdecoded(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    names = [
        "кириллица.txt",
        "with spaces.txt",
        'quo"te.txt',
        "tab\tname.txt",
        "-leading-dash.txt",
        "newline\nname.txt",
    ]
    for n in names:
        (repo / n).write_bytes(b"base\n")
    _commit(repo)
    for n in names:
        (repo / n).write_bytes(b"changed content\n")

    repo_view = gm.GitRepository(repo)
    records = {r.path: r for r in repo_view.changes()}
    for n in names:
        raw = n.encode("utf-8")
        assert raw in records, f"path {raw!r} not in change set: {list(records)!r}"
        rec = records[raw]
        # no strip, no C-quote decoding: the bytes are exactly the file name
        assert rec.path == raw
        assert b"\\" not in rec.path  # would betray C-decoding
    # digests reflect the real bytes: two look-alike names differ
    cyr, spaced = names[0], names[1]
    a = records[cyr.encode("utf-8")]
    b = records[spaced.encode("utf-8")]
    assert a.digest() != b.digest()


def test_display_path_is_lossless_and_ui_escape_is_separate(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    weird = "tab\tname.txt"
    (repo / weird).write_bytes(b"x\n")
    _commit(repo)
    (repo / weird).write_bytes(b"y\n")

    rec = gm.GitRepository(repo).changes()[0]
    # display decoding must round-trip back to the exact bytes
    assert rec.display_path.encode("utf-8", "surrogateescape") == rec.path
    # UI escaping is a separate concern and must not equal the raw name
    escaped = gm.escape_for_ui(rec.path)
    assert escaped != rec.display_path
    assert "\t" not in escaped


# ─── BaselineManifest: immutable, versioned, stable capture ──────────────


def test_baseline_manifest_is_frozen_and_versioned(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)

    manifest = gm.GitRepository(repo).capture_baseline()
    assert isinstance(manifest, gm.BaselineManifest)
    assert manifest.version == gm.SCHEMA_VERSION
    assert len(manifest.head) == 40
    with pytest.raises(Exception):
        manifest.head = "x"  # frozen dataclass — immutable
    # versioned: a round-trip through a plain dict preserves the facts
    restored = gm.BaselineManifest.from_dict(manifest.to_dict())
    assert restored == manifest


def test_capture_detects_unstable_tree_and_refuses(tmp_path, monkeypatch):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)

    calls = {"n": 0}

    def _flapping(_repo):
        calls["n"] += 1
        return f"fingerprint-{calls['n']}"

    monkeypatch.setattr(gm, "tree_fingerprint", _flapping)
    with pytest.raises(gm.UnstableTreeError):
        gm.GitRepository(repo).capture_baseline(max_attempts=3)
    # bounded retry: it did not loop forever
    assert calls["n"] <= 4


# ─── explicit refusals before any side effect ────────────────────────────


def test_unmerged_index_refused(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"base\n")
    _commit(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "f.txt").write_bytes(b"side\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "-")  # main/master
    (repo / "f.txt").write_bytes(b"main\n")
    _commit(repo, "main")
    _git(repo, "merge", "side", check=False)  # conflict

    view = gm.GitRepository(repo)
    with pytest.raises(gm.UnmergedIndexError):
        view.preflight()


def test_unmerged_refusal_escapes_control_chars_in_names(tmp_path):
    """A refusal message is a UI surface: a control character in the
    conflicted name is shown escaped (``\\t``), never leaked raw into the
    log. AR-2: the message used to carry the raw ``display_path`` bytes."""
    gm = _model()
    repo = _repo(tmp_path)
    weird = "tab\tname.txt"
    (repo / weird).write_bytes(b"base\n")
    _commit(repo)
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / weird).write_bytes(b"side\n")
    _commit(repo, "side")
    _git(repo, "checkout", "-q", "-")
    (repo / weird).write_bytes(b"main\n")
    _commit(repo, "main")
    _git(repo, "merge", "side", check=False)  # conflict

    with pytest.raises(gm.UnmergedIndexError) as exc:
        gm.GitRepository(repo).preflight()
    assert "\\t" in str(exc.value)
    assert "\t" not in str(exc.value)


def test_sparse_checkout_refused(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    _git(repo, "config", "core.sparseCheckout", "true")

    with pytest.raises(gm.UnsupportedSparseCheckoutError):
        gm.GitRepository(repo).preflight()


def test_split_index_refused(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    _git(repo, "config", "core.splitIndex", "true")

    with pytest.raises(gm.UnsupportedSplitIndexError):
        gm.GitRepository(repo).preflight()


def test_gitlink_submodule_refused_in_changes(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    # a real gitlink (submodule) index entry — mode 160000
    _git(repo, "update-index", "--add", "--cacheinfo", "160000", head, "sub")

    # a gitlink has no ownership/typechange semantics in R2.1: refuse it
    # explicitly instead of reading it as an ordinary content change
    with pytest.raises(gm.UnsupportedChangeError):
        gm.GitRepository(repo).changes()


# ─── change classification table ─────────────────────────────────────────


def test_classifies_modechange_binary_typechange_delete_add_copy_empty_crlf(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "chmod.txt").write_bytes(b"same\n")
    (repo / "bin.dat").write_bytes(b"\x00\x01\x02")
    (repo / "link-target.txt").write_bytes(b"target\n")
    (repo / "to-delete.txt").write_bytes(b"bye\n")
    (repo / "copy-src.txt").write_bytes(b"copy me\n")
    (repo / "crlf.txt").write_bytes(b"line\n")
    _commit(repo)
    _git(repo, "config", "status.renames", "copies")

    # chmod 0644 -> 0755 (mode change, content identical)
    os.chmod(repo / "chmod.txt", 0o755)
    # binary content change
    (repo / "bin.dat").write_bytes(b"\x00\xff\x00\xff")
    # regular -> symlink (typechange)
    (repo / "link-target.txt").unlink()
    os.symlink("elsewhere", repo / "link-target.txt")
    # delete + add (no rename pair)
    (repo / "to-delete.txt").unlink()
    (repo / "added-fresh.txt").write_bytes(b"new\n")
    # copy (staged)
    (repo / "copy-dst.txt").write_bytes(b"copy me\n")
    _git(repo, "add", "copy-dst.txt")
    # empty file
    (repo / "empty.txt").write_bytes(b"")
    # CRLF content
    (repo / "crlf.txt").write_bytes(b"line\r\n")

    view = gm.GitRepository(repo)
    view.preflight()
    by_path = {r.path: r for r in view.changes()}

    assert by_path[b"chmod.txt"].kind == "modified"
    assert by_path[b"chmod.txt"].mode_change is True
    assert by_path[b"bin.dat"].kind == "modified"
    assert by_path[b"link-target.txt"].kind == "typechange"
    assert by_path[b"to-delete.txt"].kind == "deleted"
    assert by_path[b"added-fresh.txt"].kind in ("added", "untracked")
    assert by_path[b"copy-dst.txt"].kind == "copied"
    assert by_path[b"copy-dst.txt"].old_path == b"copy-src.txt"
    assert by_path[b"empty.txt"].kind in ("added", "untracked")
    assert by_path[b"crlf.txt"].kind == "modified"


# ─── rename is one operation, ownership from baseline (S2-01) ────────────


def test_rename_is_a_single_operation_with_both_paths(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    _git(repo, "mv", "src.txt", "dst.txt")

    ops = gm.operations_from_changes(gm.GitRepository(repo).changes())
    renames = [op for op in ops if op.kind == "renamed"]
    assert len(renames) == 1
    assert set(renames[0].paths) == {b"src.txt", b"dst.txt"}
    assert renames[0].old_path == b"src.txt"
    assert renames[0].new_path == b"dst.txt"


def test_foreign_staged_rename_excluded_as_a_whole(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    (repo / "mine.txt").write_bytes(b"base\n")
    _commit(repo)

    # foreign WIP: a rename the user staged BEFORE the unit's baseline
    _git(repo, "mv", "src.txt", "dst.txt")
    baseline = gm.GitRepository(repo).capture_baseline()

    # the unit now changes its own file
    (repo / "mine.txt").write_bytes(b"unit edit\n")

    view = gm.GitRepository(repo)
    ops = gm.operations_from_changes(view.changes())
    result = gm.attribute_ownership(baseline, ops)

    owned = {p for op in result.owned for p in op.paths}
    foreign = {p for op in result.foreign for p in op.paths}
    assert b"mine.txt" in owned
    # the rename pair is foreign as a WHOLE: neither dst nor src is owned
    assert b"dst.txt" in foreign and b"src.txt" in foreign
    assert b"dst.txt" not in owned and b"src.txt" not in owned
    assert result.conflicts == []


def test_mixed_ownership_of_a_pair_is_a_conflict(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "src.txt").write_bytes(b"payload\n")
    _commit(repo)
    # foreign side: user edited the SOURCE, then the unit renames it
    (repo / "src.txt").write_bytes(b"foreign edit\n")
    baseline = gm.GitRepository(repo).capture_baseline()
    _git(repo, "mv", "src.txt", "dst.txt")

    view = gm.GitRepository(repo)
    ops = gm.operations_from_changes(view.changes())
    result = gm.attribute_ownership(baseline, ops)

    pairs = {frozenset(op.paths) for op in result.conflicts}
    assert frozenset({b"src.txt", b"dst.txt"}) in pairs


# ─── untracked: don't sweep secrets/ignored with a global add -A ─────────


def test_untracked_secrets_and_ignored_are_excluded_with_reason(tmp_path):
    gm = _model()
    repo = _repo(tmp_path)
    (repo / ".gitignore").write_bytes(b"build/\n")
    _commit(repo)
    (repo / ".env").write_bytes(b"SECRET=1\n")
    (repo / "normal.py").write_bytes(b"print(1)\n")
    (repo / "build").mkdir()
    (repo / "build" / "artifact.bin").write_bytes(b"x")

    manifest = gm.GitRepository(repo).capture_baseline()
    untracked = {u.path for u in manifest.untracked}
    excluded = {path for path, _reason in manifest.excluded}
    assert b"normal.py" in untracked
    assert b".env" not in untracked and b".env" in excluded
    assert b"normal.py" not in excluded
