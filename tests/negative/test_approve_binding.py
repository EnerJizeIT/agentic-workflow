"""ORCH M2.1: привязка approve к поколению забега и проверенному набору файлов.

Разрыв (воспроизведён до фикса): approve публикует ``APPROVE-{todo}.ready``
и ``VERIFIED-{todo}.sha``, не записывая поколение забега, в котором решение
принято, а commit gate не сверяет одобрение ни с поколением забега, ни с
набором файлов. После force-рестарта забега (бамп поколения, сброс
журнала ``outcomes``) оставшееся одобрение устаревшего поколения
разблокировало коммит нового цикла: вердикт записан для мёртвого цикла,
журнал уже сброшен, A-04 нечего сверять, A-15 проходит (дерево не
двигалось). Второй разрыв того же класса: в активном забеге bare ACK (или
APPROVE без привязки — ручной touch / старая версия) открывает commit gate
в auto-режиме — evidence-гейт AUD11-03 стоит только в интерактивном
ожидании, не в гейте.

Инварианты:
1. APPROVE в активном забеге несёт привязку в сигнальном файле:
   поколение + ``verified_sha`` + ``files_digest`` (дайджест того же
   набора файлов, который применяет гейт). Вне забега — сигнал пустой,
   как раньше.
2. Одобрение устаревшего поколения не разблокирует коммит: гейт отказывает
   с внятным текстом, ничего не коммитится и не стейджится, состояние не
   теряется.
3. В активном забеге коммит авторизует только bound APPROVE: ACK (и
   APPROVE без привязки) — отказ; расхождение привязки (поколение /
   отпечаток / набор файлов) — отказ.
4. Счастливый путь не меняется: approve в текущем поколении → коммит;
   вне забега — прежнее поведение.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from awf import api, git_utils, run_state
from awf.commit_gate import maybe_commit

TODO = "TODO-0001"


def _project(tmp_git_repo: Path) -> Path:
    api.init_project(tmp_git_repo, project_name="ApproveBinding")
    return tmp_git_repo


def _write_todo(proj: Path, todo_id: str = TODO, body: str = "# Task\n") -> None:
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{todo_id}.md").write_text(body, encoding="utf-8")


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


def _verify_state(proj: Path) -> str:
    """Проект в состоянии verify: работа юнита в дереве, baseline записан
    на диск и в context (как оставляют dispatch/run_next). Возвращает
    baseline sha."""
    _write_todo(proj)
    (proj / "file.txt").write_text("v1\n")
    _git_commit_all(proj, "baseline")
    baseline_sha = _head(proj)
    (proj / "file.txt").write_text("v2 good\n")  # работа юнита
    ctx = proj / ".agentic" / "context"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / f"BASELINE-{TODO}.sha").write_text(baseline_sha + "\n", encoding="utf-8")
    return baseline_sha


def _gate(proj: Path, baseline_sha: str, auto: bool = True):
    return maybe_commit(
        "verify", TODO, "commit_and_next", proj, _logs(proj),
        auto=auto, baseline_sha=baseline_sha,
    )


def _assert_tree_intact(proj: Path, baseline_sha: str) -> None:
    assert _head(proj) == baseline_sha, "HEAD moved — a stale approval committed"
    cached = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=proj, capture_output=True
    )
    assert cached.returncode == 0, "unit files were staged"
    assert (proj / "file.txt").read_text(encoding="utf-8") == "v2 good\n"


def test_approve_refused_on_stale_generation(tmp_git_repo):
    """(красный до фикса) Забег перезапущен (force) после approve:
    оставшееся одобрение устаревшего поколения не разблокирует коммит
    нового цикла."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)

    api.run_start(proj, queue=[TODO])  # поколение 1
    api.approve_commit(
        proj, TODO,
        evidence="pytest -q → 42 passed; verdict: approve",
        verified_sha=git_utils.tree_fingerprint(proj),
    )
    assert (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").is_file()

    # Забег ревизован/перезапущен: поколение 2, журнал сброшен.
    api.run_start(proj, queue=[TODO], force=True)
    assert run_state.generation_of(run_state.read_run(proj)) == 2

    ok = _gate(proj, baseline_sha)

    assert ok.status == "refused", (
        f"the commit gate committed on an approval of a dead generation — "
        f"got {ok.status}: {ok.reason}"
    )
    assert "generation" in ok.reason, f"refusal must name the generation gap: {ok.reason}"
    _assert_tree_intact(proj, baseline_sha)


def test_approve_carries_binding_and_commits(tmp_git_repo):
    """(счастливый путь) Approve в текущем поколении записывает в сигнал
    привязку (поколение, verified_sha, files_digest) — и гейт коммитит."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)
    api.run_start(proj, queue=[TODO])

    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(
        proj, TODO,
        evidence="pytest -q → 42 passed; verdict: approve",
        verified_sha=fp,
    )

    signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    binding = json.loads(signal.read_text(encoding="utf-8"))
    assert binding["generation"] == run_state.generation_of(run_state.read_run(proj)) == 1
    assert binding["verified_sha"] == fp
    assert len(binding["files_digest"]) == 64

    ok = _gate(proj, baseline_sha)
    assert ok.status == "committed", (
        f"the verified success path must commit — got {ok.status}: {ok.reason}"
    )
    assert _head(proj) != baseline_sha


def test_fresh_approve_after_run_replace_commits(tmp_git_repo):
    """(нет ложного отказа) После force-рестарта забега НОВЫЙ approve
    (новое поколение) коммитит как обычно — привязка сверяется с
    текущим поколением, а не с прошлым."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    api.approve_commit(
        proj, TODO,
        evidence="probes ok",
        verified_sha=git_utils.tree_fingerprint(proj),
    )
    api.run_start(proj, queue=[TODO], force=True)  # поколение 2

    api.approve_commit(
        proj, TODO,
        evidence="probes ok (fresh run)",
        verified_sha=git_utils.tree_fingerprint(proj),
    )

    ok = _gate(proj, baseline_sha)
    assert ok.status == "committed", f"the fresh approval must commit — got {ok.status}: {ok.reason}"
    assert _head(proj) != baseline_sha


def test_ack_does_not_unlock_commit_in_run(tmp_git_repo):
    """ACK без bound APPROVE не авторизует коммит в активном забеге:
    за ним нет ни evidence, ни отпечатка (bypass того же класса, что
    и stale approve)."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    (proj / ".agentic" / "inbox" / f"ACK-{TODO}.ready").touch()

    ok = _gate(proj, baseline_sha)

    assert ok.status == "refused", (
        f"the commit gate committed on a bare ACK in an active run — got {ok.status}: {ok.reason}"
    )
    _assert_tree_intact(proj, baseline_sha)


def test_bare_approve_in_run_refused(tmp_git_repo):
    """APPROVE без привязки (ручной touch / старая версия awf) не
    авторизует коммит в активном забеге — требуется повторный
    awf_approve."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    (proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready").touch()

    ok = _gate(proj, baseline_sha)

    assert ok.status == "refused", (
        f"the commit gate committed on an unbound APPROVE in an active run — got {ok.status}: {ok.reason}"
    )
    _assert_tree_intact(proj, baseline_sha)


def test_files_digest_mismatch_refused(tmp_git_repo):
    """Подменённый files_digest в привязке — гейт отказывает: набор
    файлов коммита отличается от покрытого approve."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)
    api.run_start(proj, queue=[TODO])
    api.approve_commit(
        proj, TODO,
        evidence="probes ok",
        verified_sha=git_utils.tree_fingerprint(proj),
    )
    signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    binding = json.loads(signal.read_text(encoding="utf-8"))
    binding["files_digest"] = "0" * 64
    signal.write_text(json.dumps(binding), encoding="utf-8")

    ok = _gate(proj, baseline_sha)

    assert ok.status == "refused", (
        f"the commit gate committed on a tampered file-set digest — got {ok.status}: {ok.reason}"
    )
    assert "digest" in ok.reason, f"refusal must name the digest mismatch: {ok.reason}"
    _assert_tree_intact(proj, baseline_sha)


def test_outside_run_signal_stays_empty_and_commits(tmp_git_repo):
    """(регрессия) Вне забега: сигнал остаётся пустым (touch), гейт
    коммитит — legacy-поведение не тронуто."""
    proj = _project(tmp_git_repo)
    baseline_sha = _verify_state(proj)

    api.approve_commit(
        proj, TODO,
        verified_sha=git_utils.tree_fingerprint(proj),
    )

    signal = proj / ".agentic" / "inbox" / f"APPROVE-{TODO}.ready"
    assert signal.is_file()
    assert signal.read_text(encoding="utf-8").strip() == "", (
        "outside a run the APPROVE signal must stay an empty marker"
    )

    ok = _gate(proj, baseline_sha)
    assert ok.status == "committed", f"the no-run path must commit — got {ok.status}: {ok.reason}"
    assert _head(proj) != baseline_sha
