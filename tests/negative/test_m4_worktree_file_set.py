"""QA-0228: regression pins for ``commit_delta.worktree_file_set``.

The no-baseline file set moved from the printed ``status --porcelain -z``
token parser to the R2.1 model (``changes()`` + ``untracked_paths()``). The
legacy rules that the old parser encoded are pinned here on the new surface:

* a worktree-only deletion stays IN (REVIEW-0104 F1);
* the user's index WIP stays OUT (R-03: a staged change is foreign);
* a staged rename contributes neither of its two paths (REVIEW-0087);
* untracked content is listed as FILES, never as a directory entry.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "qa@example.com")
    _git(root, "config", "user.name", "QA")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "victim.txt").write_text("delete me\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def test_worktree_deletion_and_untracked_stay_in(repo: Path) -> None:
    from awf.commit_delta import worktree_file_set

    (repo / "victim.txt").unlink()
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    assert set(worktree_file_set(repo)) == {"victim.txt", "new.txt"}


def test_staged_changes_stay_out(repo: Path) -> None:
    from awf.commit_delta import worktree_file_set

    (repo / "keep.txt").write_text("edited\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    (repo / "victim.txt").unlink()
    _git(repo, "add", "-A")
    (repo / "unit.txt").write_text("unit\n", encoding="utf-8")

    assert worktree_file_set(repo) == ["unit.txt"]


def test_staged_rename_contributes_neither_path(repo: Path) -> None:
    from awf.commit_delta import worktree_file_set

    _git(repo, "mv", "keep.txt", "moved.txt")
    (repo / "moved.txt").write_text("rename plus edit\n", encoding="utf-8")
    (repo / "unit.txt").write_text("unit\n", encoding="utf-8")

    assert worktree_file_set(repo) == ["unit.txt"]


def test_untracked_directory_is_listed_as_files(repo: Path) -> None:
    from awf.commit_delta import worktree_file_set

    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "sub" / "b.txt").write_text("b\n", encoding="utf-8")

    assert sorted(worktree_file_set(repo)) == ["sub/a.txt", "sub/b.txt"]


def test_non_utf8_untracked_name_survives_as_raw_identity(repo: Path) -> None:
    from awf.commit_delta import worktree_file_set

    _git(repo, "config", "core.quotepath", "false")
    name = os.fsdecode(b"tot-\xff\xfe.txt")
    (repo / name).write_text("x\n", encoding="utf-8")

    assert worktree_file_set(repo) == [name]
