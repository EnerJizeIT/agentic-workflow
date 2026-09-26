"""A-04 (аудит 2026-09-25, слой 4): конфликт reject/approve не оставляет
разрешающий сигнал.

Дефект: ``approve_commit`` создавал ``APPROVE-<id>.ready`` ДО атомарной
записи решения; при уже записанном reject verdict оставался ``rejected``,
API сообщало о конфликте, но файл APPROVE оставался — и commit gate
(ждёт APPROVE/ACK без сверки verdict) мог прокоммитить rejected-юнит.

Инварианты:
1. APPROVE публикуется только после успешной атомарной записи решения;
   конфликт → отказ без файла, verdict ``rejected``, прежние сигналы
   не трогаются.
2. Commit gate перед коммитом сверяет актуальный verdict активного
   забега; ``rejected`` → внятный отказ, ничего не стейджится.
3. Успешный путь approve → commit не меняется (включая режим без забега).

До фикса тесты красные — это и есть точка.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import api, run_state
from awf.commit_gate import maybe_commit

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="A04Verdict")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = TODO, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


def _verify_state(proj: Path, todo_id: str = TODO) -> None:
    """Проект в состоянии verify: юнит готов (DONE), ждёт вердикт."""
    _write_todo(proj, todo_id)
    outbox = proj / ".agentic" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"DONE-{todo_id}.ready").touch()


def _head(proj: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_commit_all(proj: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=proj, check=True)


def test_reject_then_approve_leaves_no_approve_signal(tmp_git_repo):
    """Конфликт (reject записан раньше) → approve отказывается БЕЗ файла
    APPROVE; verdict остаётся rejected, счётчик не меняется."""
    proj = _project(tmp_git_repo)
    _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    api.reject_commit(proj, TODO, "defect: off-by-one in the gate")

    result = api.approve_commit(proj, TODO, evidence="late approve after reject")

    inbox = proj / ".agentic" / "inbox"
    assert not (inbox / f"APPROVE-{TODO}.ready").exists(), (
        "conflicting approve left APPROVE-*.ready — the commit gate would "
        "unlock on a rejected verdict"
    )
    assert result.signal_file == ""
    state = run_state.read_run(proj)
    assert state["outcomes"][TODO]["verdict"] == "rejected"
    assert state["rejects"][TODO] == 1
    assert "rejection" in result.message


def test_commit_gate_refuses_when_verdict_rejected(tmp_git_repo):
    """Остаток гонки: APPROVE-файл есть, verdict rejected → гейт не
    коммитит, ничего не стейджится, дерево не тронуто."""
    proj = _project(tmp_git_repo)
    _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    api.reject_commit(proj, TODO, "defect: off-by-one in the gate")

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)

    # остаток проигранной гонки: сигнал approve при записанном reject
    inbox = proj / ".agentic" / "inbox"
    (inbox / f"APPROVE-{TODO}.ready").touch()
    (proj / "file.txt").write_text("v2 broken\n")

    ok = maybe_commit(
        "verify", TODO, "commit_and_next", proj,
        proj / ".agentic" / "logs",
        auto=True, baseline_sha=baseline_sha,
    )

    assert ok is False, "commit gate committed a rejected unit"
    assert _head(proj) == baseline_sha, "HEAD moved — the rejected unit was committed"
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=proj, capture_output=True, text=True, check=True
    )
    assert "file.txt" in status.stdout, "working tree was touched"
    cached = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=proj, capture_output=True
    )
    assert cached.returncode == 0, "unit files were staged"


def test_approve_then_commit_still_commits(tmp_git_repo):
    """Регрессия (инвариант 3): approve → verdict approved → commit
    проходит, юнит фиксируется коммитом."""
    proj = _project(tmp_git_repo)
    _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    api.approve_commit(proj, TODO, evidence="probes ok")

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n")

    ok = maybe_commit(
        "verify", TODO, "commit_and_next", proj,
        proj / ".agentic" / "logs",
        auto=True, baseline_sha=baseline_sha,
    )

    assert ok is True
    assert _head(proj) != baseline_sha
    assert run_state.read_run(proj)["outcomes"][TODO]["verdict"] == "approved"


def test_commit_without_run_commits_as_before(tmp_git_repo):
    """Регрессия: без забега вердикта нет — гейт ведёт себя как раньше."""
    proj = _project(tmp_git_repo)  # .agentic есть, run.yaml нет

    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2\n")

    ok = maybe_commit(
        "verify", "TODO-0099", "commit_and_next", proj,
        proj / ".agentic" / "logs",
        auto=False, baseline_sha=baseline_sha,
    )

    assert ok is True
    assert _head(proj) != baseline_sha
