"""R2.2 QA (TODO-0221, iter 5): the R2.1 raw rework broke the leak-gate's
"never raises" contract on a TRANSIENT git failure.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, iter 5).

M4 review: the leak-gate reads untracked paths with ONE git call —
``GitRepository.untracked_paths()`` (the NUL protocol). The old second printed
scan and its ``raw -> printed`` map are gone: no consumer used the map, and a
printed scan is a second implementation of the same fact.

The contract this file pins is unchanged and now lives on the live surface:

* ``snapshot_rejected_files`` — «Never raises — the reject must not fail on
  it» (RUN5 #1 Part A.1): a hung/missing git degrades to ``[]``;
* ``orphaned_reject_files`` — «[] when … git cannot measure»
  (failsafe-поверхность verify-pack / approve);
* ``core.quotepath=false`` + non-UTF-8 name is not a failure at all: the model
  returns the raw path, so the finding is recorded instead of dropped.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from awf import api, paths, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"
FINDING = "finding.txt"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed disposable repo with an awf skeleton."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "tester")
    api.init_project(
        r, project_name="R2QA5", test_cmd="", lint_cmd="",
        typecheck_cmd="", build_cmd="",
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "awf init")
    return r


@pytest.fixture
def untracked_read_fails(monkeypatch):
    """The single model read fails, as a hung/missing git would."""
    from awf.infra.git_model import GitModelError, GitRepository

    def boom(self):
        raise GitModelError("git ls-files timed out")

    monkeypatch.setattr(GitRepository, "untracked_paths", boom)


def test_snapshot_never_raises_on_transient_git_failure(repo, untracked_read_fails):
    """A hung git on the untracked read must not fail the reject: the snapshot
    is best-effort and degrades to ``[]`` (RUN5 #1 Part A.1)."""
    (repo / FINDING).write_text("finding\n", encoding="utf-8")
    recorded = reject_files.snapshot_rejected_files(
        repo, SRC, paths.logs_dir(repo)
    )
    assert recorded == [], (
        "snapshot_rejected_files must swallow a git failure on the untracked "
        f"read and return []; got {recorded!r}"
    )


def test_orphaned_never_raises_on_transient_git_failure(repo, untracked_read_fails):
    """The failsafe must return ``[]`` when git cannot measure, not raise."""
    (repo / FINDING).write_text("finding\n", encoding="utf-8")
    ctx = paths.context_dir(repo)
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"REJECT-{SRC}.files").write_text(FINDING + "\n", encoding="utf-8")
    (ctx / f"BASELINE-{RETRY}.untracked").write_text(FINDING + "\n", encoding="utf-8")

    result = reject_files.orphaned_reject_files(repo, RETRY)
    assert result == [], (
        "orphaned_reject_files must return [] when git cannot measure; "
        f"got {result!r}"
    )


def test_snapshot_reads_non_utf8_under_quotepath_false(repo):
    """A DETERMINISTIC trigger, no failure injection: with ``core.quotepath=false``
    (a legitimate user config) ``git ls-files`` emits raw non-UTF-8 bytes. The
    model returns them as the raw identity, so the reject records the finding
    instead of dropping it."""
    import os

    _git(repo, "config", "core.quotepath", "false")
    name = b"\xff\xfe.txt"
    fd = os.open(os.fsencode(repo) + b"/" + name, os.O_CREAT | os.O_WRONLY, 0o644)
    os.write(fd, b"finding\n")
    os.close(fd)

    recorded = reject_files.snapshot_rejected_files(
        repo, SRC, paths.logs_dir(repo)
    )
    assert recorded == [os.fsdecode(name)], (
        "snapshot_rejected_files must keep the raw non-UTF-8 finding under "
        f"core.quotepath=false; got {recorded!r}"
    )
