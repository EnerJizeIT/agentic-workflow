"""R2.1 architect-review, iter 10 (roadmap R2, unit R2.1): the "versioned"
manifest actually rejects a schema version it does not know.

``BaselineManifest`` is documented as an immutable, VERSIONED snapshot, but
``from_dict`` read ``version`` and never compared it to ``SCHEMA_VERSION``: a
payload written by another schema would be parsed as the current one and its
ownership facts silently mis-read — the silent-legacy class the module exists
to remove, a sibling of the unmerged / sparse / gitlink refusals.

Locked here:

* a foreign version (newer AND older) refuses with a typed error, BEFORE any
  fact is accepted;
* the supported version still round-trips to an equal, frozen manifest;
* the deliberate split of the untracked readers is pinned: the ownership
  readers EXCLUDE a secret-looking name, the stability fingerprint INCLUDES
  its content (a secret edit still moves the baseline).

All repositories are disposable in ``tmp_path``; real git is never touched.
"""
from __future__ import annotations

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


def _manifest(gm, tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "a.txt").write_bytes(b"a\n")
    _commit(repo)
    return gm.GitRepository(repo).capture_baseline()


def test_manifest_from_dict_refuses_a_foreign_schema_version(tmp_path):
    """`versioned` means the reader REFUSES a version it does not know, not
    that it silently parses it as the current snapshot."""
    gm = _model()
    manifest = _manifest(gm, tmp_path)
    payload = manifest.to_dict()

    payload["version"] = gm.SCHEMA_VERSION + 1
    with pytest.raises(gm.UnsupportedManifestVersionError):
        gm.BaselineManifest.from_dict(payload)

    # an OLDER version is foreign too: this reader would mis-read its facts,
    # so it is refused rather than migrated silently — migration dispatches
    # on the version before calling from_dict.
    payload["version"] = gm.SCHEMA_VERSION - 1
    with pytest.raises(gm.GitModelError):
        gm.BaselineManifest.from_dict(payload)


def test_current_version_still_round_trips(tmp_path):
    """The guard does not break the supported schema: the current version
    round-trips to an equal, frozen manifest."""
    gm = _model()
    manifest = _manifest(gm, tmp_path)
    restored = gm.BaselineManifest.from_dict(manifest.to_dict())
    assert restored == manifest
    assert restored.version == gm.SCHEMA_VERSION


def test_secret_is_excluded_from_ownership_but_hashed_by_stability(tmp_path):
    """Pin the documented split of the untracked readers (F-QA4 scope): the
    manifest EXCLUDES a secret-looking name, the stability fingerprint
    INCLUDES its content — a secret edit still moves the baseline."""
    gm = _model()
    repo = _repo(tmp_path)
    (repo / "keep.txt").write_bytes(b"k\n")
    _commit(repo)
    (repo / ".env").write_bytes(b"SECRET=v1\n")

    manifest = gm.GitRepository(repo).capture_baseline()
    excluded = {path: reason for path, reason in manifest.excluded}
    assert b".env" not in {u.path for u in manifest.untracked}
    assert excluded.get(b".env") == "secret"

    before = gm.tree_fingerprint(repo)
    (repo / ".env").write_bytes(b"SECRET=v2 changed\n")
    assert gm.tree_fingerprint(repo) != before, (
        "смена секрета не изменила отпечаток — страж стабильности потерял "
        "содержимое всего дерева"
    )
