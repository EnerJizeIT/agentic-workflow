"""R2.5 (roadmap R2) — QA-проба идентичности фактов, шестой блок.

Класс: «неинтерпретируемый факт — не факт» и «committed ⇒ OID этого юнита».
Оба факта публикации адресуются ИМЕНЕМ файла (``PUBLISH-INTENT-<todo>`` /
``RECEIPT-<todo>``). Тело факта тоже несёт ``todo_id``, и до этого витка
читатели его не сверяли с именем. Копия/восстановление факта под чужим
именем (backup, ручной перенос, копипаст между юнитами) описывает ДРУГУЮ
операцию, но читается как операция этого юнита:

* intent с чужим ``todo_id`` резюмится под аргументом, пишет receipt под
  телом и НЕ снимает intent-файл аргумента — вечный pending, повторное
  «committed» на каждом вызове, артефакт юнита без receipt;
* receipt с чужим ``todo_id`` авторизует архив юнита, чьей работы в HEAD
  нет — ровно тот ложный полный успех, который receipt и должен исключать
  (требование 3: archived только после committed ЭТОГО юнита).

Оба репро — disposable-репозитории в ``tmp_path``; прод-код не правится
(находки уходят супервизору).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

TODO = "TODO-0001"
FOREIGN = "TODO-0002"


def _publish():
    from awf import publish

    return publish


def _git(repo: Path, *args: str):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )


def _repo(tmp_path: Path) -> Path:
    from awf import paths

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    (repo / ".agentic" / "logs").mkdir(parents=True)
    paths.context_dir(repo).mkdir(parents=True, exist_ok=True)
    return repo


def _intent(repo: Path, body_todo: str, commit_oid: str, head: str) -> dict:
    return {
        "todo_id": body_todo,
        "generation": 0,
        "expected_head": head,
        "tree_oid": "",
        "commit_oid": commit_oid,
        "candidate_tree": "",
        "files": ["README.md"],
        "delta_files": [],
        "degraded": [],
        "message": "x",
        "index_orig": {},
        "evidence_refs": [],
        "transfers": [],
        "residuals": [],
        "created_at": "",
    }


def test_intent_naming_another_unit_is_not_a_pending_operation(tmp_path):
    """Intent-файл TODO-0001 с телом TODO-0002 не описывает операцию 0001.

    Он не должен ни резюмиться под 0001, ни писать receipt под 0002, ни
    оставлять 0001 в вечном pending. До фикса: ``committed``, receipt
    ложится в ``RECEIPT-TODO-0002.json``, intent TODO-0001 остаётся, и
    каждый следующий вызов повторяет тот же хвост."""
    publish = _publish()
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    body = _intent(repo, FOREIGN, head, head)
    (repo / ".agentic" / "context" / f"PUBLISH-INTENT-{TODO}.json").write_text(
        json.dumps(body), encoding="utf-8"
    )

    assert publish.read_intent(repo, TODO) is None, (
        "a fact whose body names another unit cannot describe THIS unit's "
        "pending operation — the same rule that drops a malformed OID"
    )
    out = publish.resume_if_pending(repo, TODO)
    assert out is None, (
        "a foreign-todo intent must not resume (and must not move the ref) — "
        f"got {out}"
    )
    assert not publish.receipt_file(repo, FOREIGN).exists(), (
        "resuming under TODO-0001 must never write a receipt for TODO-0002"
    )


def test_receipt_naming_another_unit_does_not_authorize_archive(tmp_path):
    """Receipt-файл TODO-0001 с телом TODO-0002 не авторизует архив 0001.

    Требование 3: архив стоит на receipt ЭТОГО юнита. Копия чужого receipt
    под именем юнита не доказывает, что работа юнита в HEAD; до фикса она
    и показывает чужой commit в status, и открывает архив."""
    publish = _publish()
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    receipt = {
        "todo_id": FOREIGN,
        "generation": 0,
        "disposition": "committed",
        "commit_oid": head,
        "candidate_tree": "",
        "expected_head": "",
        "evidence_refs": [],
        "transfers": [],
        "residuals": [],
        "degraded": [],
        "index_sync": "synced",
        "files": [],
        "created_at": "",
    }
    ctx = repo / ".agentic" / "context"
    (ctx / f"RECEIPT-{TODO}.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    (repo / ".agentic" / "inbox").mkdir(parents=True, exist_ok=True)
    (repo / ".agentic" / "inbox" / f"{TODO}.md").write_text(
        "# TODO\n", encoding="utf-8"
    )

    assert publish.read_receipt(repo, TODO) is None, (
        "a receipt that claims to be another unit's cannot authorize THIS "
        "unit — committed ⇒ an OID of the unit the receipt names"
    )
    try:
        archived = publish.archive_after_receipt(repo, TODO)
    except publish.PublishError:
        archived = None
    assert archived is None, (
        "a foreign-todo receipt must not authorize archiving TODO-0001; "
        f"got archive at {archived}"
    )
    assert not (repo / ".agentic" / "done" / TODO).exists(), (
        "TODO-0001 was archived without a receipt of its own"
    )


def test_hook_stderr_with_non_utf8_bytes_does_not_crash_the_publication(tmp_path):
    """Хук, печатающий байт-верный путь в stderr, не роняет публикацию.

    ``_git_isolated`` гоняет КАЖДУЮ git-команду и каждый хук с
    ``text=True`` (строгий UTF-8). Хук с ``printf`` не-UTF-8 байта
    (легальное имя файла доезжает до ``echo "$file"``) валит чтение
    ``UnicodeDecodeError`` — это ``ValueError``, не ``SubprocessError``/
    ``OSError``: он проходит мимо всех типизированных границ публикации и
    мимо ``(RuntimeError, TimeoutError)`` пайплайна. HEAD не двигается,
    intent (якорь возобновления) не пишется, стадия падает сырым
    трейсбеком — та же семья, что Q5-1, но на обратном направлении."""
    from awf import commit_gate, commit_plan

    repo = _repo(tmp_path)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_bytes(b'#!/bin/sh\nprintf "bad\\377name\\n" >&2\nexit 0\n')
    hook.chmod(0o755)

    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    plan = commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=("README.md",)
    )
    try:
        out = commit_gate._commit_via_isolated_index(
            repo, plan, f"awf(verify): {TODO}", repo / ".agentic" / "logs"
        )
    except Exception as e:  # noqa: BLE001 — любой не-типизированный исход = дефект
        import pytest

        pytest.fail(
            "a hook writing a non-UTF-8 byte to stderr crashed the "
            f"publication with an untyped {type(e).__name__}: {e} — the "
            "shared git runner must decode hook output losslessly"
        )
    assert out.status == "committed", (
        f"a successful hook with byte output must still publish; got "
        f"{out.status}: {out.reason}"
    )
