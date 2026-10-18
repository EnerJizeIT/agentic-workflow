"""R2.2 architect-review, iter 8 (TODO-0221): the M3 guard covered two of the
three printed ``git_stdout`` reads in ``create_baseline`` — the third
(``git stash create``, the dirty-worktree snapshot) is still bare.

Файл КРАСНЫЙ на дереве до фикса стадии agent-architect-review.

M3 (iter 7) обернул тройным guard'ом ``SubprocessError``/``OSError``/
``UnicodeDecodeError`` печатное чтение ``git status --short`` и листинг
untracked в ``_untracked_snapshot`` — чтобы зависший/пропавший git НЕ валил
запись baseline (`awf dispatch` / `awf run_next`). Рядом в ТОЙ ЖЕ функции
``awf/api/pipeline.py::create_baseline`` остаётся третье печатное чтение —
``git stash create`` (снапшот грязного дерева). Его собственный комментарий
обещает «a failure never fails the baseline», но это верно только для
НЕНУЛЕВОГО rc (``check=False`` возвращает ""), а НЕ для исключения:
``subprocess.TimeoutExpired`` (зависший git) и ``OSError`` (git недоступен)
уходят наружу через внешний ``except Exception: ... raise`` и валят
``create_baseline`` — тот же класс, что закрыл M3 у соседей.

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api, git_utils

SRC = "TODO-0001"


def _git(repo: Path, *args: str, check: bool = True) -> None:
    subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True)


def _project(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2Arch4",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def test_create_baseline_survives_transient_git_on_stash_snapshot(tmp_path, monkeypatch):
    """A hung/missing git on the ``stash create`` read must degrade the
    worktree snapshot to empty, not raise out of ``create_baseline``."""
    repo = _project(tmp_path)
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "core")
    (repo / "core.py").write_text("pre-dispatch WIP\n", encoding="utf-8")

    real = git_utils.git_stdout

    def flaky(cwd, *args, **kwargs):
        # Only the dirty-worktree snapshot read fails transiently; every
        # other git read (rev-parse HEAD, status, ls-files) delegates to the
        # real one, so the test isolates the unguarded surface.
        if args and args[0] == "stash":
            raise subprocess.TimeoutExpired(cmd="git stash create", timeout=30)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(git_utils, "git_stdout", flaky)

    result = api.create_baseline(repo, SRC)
    assert result.todo_id == SRC
    assert (repo / ".agentic" / "context" / f"BASELINE-{SRC}.sha").is_file()
