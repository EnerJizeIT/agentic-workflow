"""A-15 (аудит 2026-09-25, слой 4): verified_sha защищает интервал до коммита.

Дефект: ``approve_commit(verified_sha=...)`` сравнивает переданный
отпечаток с деревом и пишет ``VERIFIED-<id>.sha``, затем публикует APPROVE
— но commit gate (``maybe_commit``) читает только сигналы и baseline; файл
VERIFIED на commit path нигде не сверяется. Изменение дерева между approve
и ``git add/commit`` попадает в коммит без повторной проверки.

Инварианты:
1. Если approve был с verified_sha (файл VERIFIED существует): commit path
   пересчитывает отпечаток дерева перед staging и непосредственно перед
   коммитом; расхождение → отказ, коммита нет, сообщение требует
   повторной верификации.
2. Если verified_sha не передавался (файла нет) — поведение как сейчас.
3. Успешный путь не меняется.

До фикса тесты красные — это и есть точка.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api, git_utils
from awf.commit_gate import maybe_commit

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A15VerifiedSha")
    return tmp_git_repo


def _head(proj: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_commit_all(proj: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=proj, check=True)


def _logs(proj: Path) -> Path:
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def test_tree_change_after_approve_blocks_commit(tmp_git_repo, capsys):
    """approve с verified_sha → дерево сместилось → гейт отказывает:
    коммита нет, ничего не стейджится, дерево не тронуто, сообщение
    требует повторной верификации."""
    proj = _project(tmp_git_repo)

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n")  # работа юнита

    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, TODO, verified_sha=fp)
    assert (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").is_file()

    # окно A-15: дерево сместилось между approve и commit gate
    (proj / "file.txt").write_text("v3 unverified\n")

    ok = maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=True, baseline_sha=baseline_sha,
    )

    assert ok.status == "refused", (
        f"commit gate committed a tree that was not verified — got {ok.status}: {ok.reason}"
    )
    assert _head(proj) == baseline_sha, "HEAD moved — an unverified change was committed"
    cached = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=proj, capture_output=True
    )
    assert cached.returncode == 0, "the unit files were staged"
    assert (proj / "file.txt").read_text(encoding="utf-8") == "v3 unverified\n", (
        "working tree was touched"
    )
    assert "re-verify" in capsys.readouterr().err.lower(), (
        "the refusal must tell the supervisor to re-verify on the current tree"
    )


def test_tree_unchanged_after_approve_commits(tmp_git_repo):
    """Регрессия (инвариант 3): дерево не менялось между approve и гейтом
    → коммит проходит, юнит фиксируется коммитом."""
    proj = _project(tmp_git_repo)

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n")

    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, TODO, verified_sha=fp)

    ok = maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=True, baseline_sha=baseline_sha,
    )

    assert ok.status == "committed", f"the verified success path must commit — got {ok.status}: {ok.reason}"
    assert _head(proj) != baseline_sha
    committed = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=proj, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "file.txt" in committed


def test_no_verified_file_legacy_behavior(tmp_git_repo):
    """Регрессия (инвариант 2): approve без verified_sha (файла нет) →
    гейт ведёт себя как раньше: смена дерева коммит не блокирует."""
    proj = _project(tmp_git_repo)

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n")

    api.approve_commit(proj, TODO)  # без verified_sha
    assert not (proj / ".agentic" / "context" / f"VERIFIED-{TODO}.sha").exists()

    (proj / "file.txt").write_text("v3\n")  # дерево сместилось — допустимо без sha

    ok = maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=True, baseline_sha=baseline_sha,
    )

    assert ok.status == "committed", f"legacy path (no VERIFIED file) must not gain a new refusal — got {ok.status}: {ok.reason}"
    assert _head(proj) != baseline_sha
