"""A-19 (аудит 2026-09-25, слой 9): пути тестов prove-red не выходят за корни.

Дефект (воспроизведён в аудите): ``_copy_test_files`` проверяет только
непустоту строки и отсутствие NUL; выражения ``project_dir / rel`` и
``worktree / rel`` допускают ``..``, абсолютный путь и переход через
симлинк. Файл вне проекта может быть прочитан, а ``shutil.copy2`` —
записать за пределы временного worktree (до запуска pytest).

Инварианты:
1. Путь теста — относительный, остаётся внутри проекта (``resolve()``
   источника против корня проекта) и внутри worktree (``resolve()``
   назначения против корня worktree, с учётом симлинков).
2. ``../``, абсолютный путь, симлинк наружу — управляемая ошибка до
   заполнения worktree; файлы вне проекта и worktree не читаются и не
   изменяются.
3. Валидные пути (включая ``file::test``) работают без изменений.

Проект — реальный ``tmp_git_repo``; атаки показываются канарейками
снаружи репозитория, которые обязан остаться нетронутыми.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from awf import api
from awf.prove_red import _copy_test_files, prove_red

RED_TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _agentic(repo: Path) -> None:
    (repo / ".agentic").mkdir(exist_ok=True)


def _baseline(repo: Path, todo_id: str = "TODO-0001") -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    ctx = repo / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{todo_id}.sha").write_text(sha + "\n", encoding="utf-8")
    return sha


def test_parent_traversal_test_path_is_rejected(tmp_git_repo: Path, tmp_path: Path) -> None:
    """``../outside.py`` — управляемый отказ; файл вне проекта не читается
    и не копируется (канарейка за корнем репозитория)."""
    repo = tmp_git_repo  # tmp_path/repo
    _agentic(repo)
    _baseline(repo)
    canary = tmp_path / "outside.py"
    canary.write_text("SECRET = 42\n", encoding="utf-8")

    with pytest.raises(api.AwfApiError):
        prove_red(repo, "TODO-0001", tests=["../outside.py"], tmp_base=tmp_path / "wt")

    # Канарейка вне проекта не тронута и не скопирована за пределы worktree.
    assert canary.read_text(encoding="utf-8") == "SECRET = 42\n"
    assert not (tmp_path / "wt" / "outside.py").exists()


def test_absolute_test_path_is_rejected(tmp_git_repo: Path, tmp_path: Path) -> None:
    """Абсолютный путь — управляемый отказ до любого чтения/записи файла."""
    repo = tmp_git_repo
    _agentic(repo)
    _baseline(repo)
    canary = tmp_path / "abs_canary.py"
    canary.write_text("SECRET = 42\n", encoding="utf-8")

    with pytest.raises(api.AwfApiError):
        prove_red(repo, "TODO-0001", tests=[str(canary)], tmp_base=tmp_path / "wt")

    # Канарейка (абсолютный путь) не тронута.
    assert canary.read_text(encoding="utf-8") == "SECRET = 42\n"


def test_symlink_outside_rejected(tmp_git_repo: Path, tmp_path: Path) -> None:
    """Симлинк на файл вне проекта отклоняется на шаге копирования; цель
    симлинка не читается и не копируется (не в prove_red — на уровне
    ``_copy_test_files``)."""
    repo = tmp_git_repo
    _agentic(repo)
    target = tmp_path / "outside_target.py"
    target.write_text("SECRET = 42\n", encoding="utf-8")
    (repo / "tests").mkdir()
    link = repo / "tests" / "link_outside.py"
    os.symlink(target, link)

    worktree = tmp_path / "wt"
    worktree.mkdir()

    with pytest.raises(api.AwfApiError):
        _copy_test_files(repo, worktree, ["tests/link_outside.py"])

    # Цель симлинка вне проекта не тронута и не скопирована в worktree.
    assert target.read_text(encoding="utf-8") == "SECRET = 42\n"
    assert not (worktree / "tests" / "link_outside.py").exists()


def test_normal_relative_path_still_works(tmp_git_repo: Path, tmp_path: Path) -> None:
    """Регресс: обычный относительный путь копируется и прогоняется как
    раньше (новый непроверенный тест, baseline — баг, дерево — фикс)."""
    repo = tmp_git_repo
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    _agentic(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: buggy add")
    _baseline(repo)

    (repo / "tests").mkdir()
    (repo / "tests" / "test_calc.py").write_text(RED_TEST)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")

    result = prove_red(
        repo, "TODO-0001", tests=["tests/test_calc.py"], tmp_base=tmp_path / "wt"
    )

    assert result.verdict == "red-ok"
    assert result.exit_code == 0
    assert result.copied_files == ["tests/test_calc.py"]


def test_dest_symlink_committed_in_baseline_rejected(tmp_git_repo: Path, tmp_path: Path) -> None:
    """Симлинк, закоммиченный в baseline (каталог tests -> каталог снаружи):
    источник — обычный файл внутри проекта, а назначение после resolve()
    выходит за пределы worktree — управляемый отказ на шаге копирования.
    Ничего не пишется за пределы worktree."""
    repo = tmp_git_repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, repo / "tests")
    _agentic(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: tests is a symlink outside")
    _baseline(repo)

    # Текущее дерево: симлинк заменён настоящим каталогом, файл теста —
    # обычный файл внутри проекта (проходит source-проверку).
    (repo / "tests").unlink()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_evil.py").write_text(
        "def test_ok():\n    assert 1 + 1 == 2\n", encoding="utf-8"
    )

    sha_file = next((repo / ".agentic" / "context").glob("BASELINE-*.sha"))
    todo_id = sha_file.name.removeprefix("BASELINE-").removesuffix(".sha")

    with pytest.raises(api.AwfApiError):
        prove_red(repo, todo_id, tests=["tests/test_evil.py"], tmp_base=tmp_path / "wt")

    # За пределы worktree ничего не записано: внешний каталог пуст.
    assert list(elsewhere.iterdir()) == []
    # Worktree не пережил отказ: в репозитории остался только главный.
    listed = subprocess.run(
        ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip().splitlines()
    assert len(listed) == 1
