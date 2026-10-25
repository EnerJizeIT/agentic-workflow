"""R2.5 (roadmap R2) — QA-проба границ, пятый блок (agent-qa-review, iter 8).

Находка того же класса, что уже закрытые D3/L1/E1: **факт публикации с
байт-верным (не-UTF-8) путём не сериализуется, и отказ летит мимо всех
типизированных границ**. R2.1 держит имена файлов как БАЙТЫ
(``surrogateescape``) — это заявленное свойство модели git, и
``commit_delta``/``commit_plan`` несут такие имена в план без отказа. R2.5
пишет план в факт-файлы через ``json.dumps(..., ensure_ascii=False)`` и
UTF-8; строка с lone-surrogate не кодируется в UTF-8, ``json`` оставляет
суррогаты как есть, и ``managed_write_text`` падает ``UnicodeEncodeError``.

``UnicodeEncodeError`` — не ``OSError`` и не ``ManagedPathError``, поэтому
``_write_json`` (ловит ``ManagedPathError``/``OSError``) его не типизирует,
а гейт публикации (ловит ``PublishError``/``SubprocessError``/``OSError``)
пропускает насквозь: стадия падает сырым трейсбеком, intent (durable-якорь
возобновления) не записан. Это регресс к пре-R2.5 ``git commit``, который
любой путь коммитил, и нарушение инварианта «публикация — возобновляемая
операция, отказ — типизированный исход».

Второй тест — та же семья на best-effort писателе аудита
(``commit_gate._write_commit_audit_record`` документирован «логирует и
идёт дальше»), который тоже пишет пути через ``ensure_ascii=False``.

Оба — на disposable-репозиториях в ``tmp_path``; реальный git не трогается,
прод-код не правится (находки уходят супервизору).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

TODO = "TODO-0001"

#: Имя с не-UTF-8 байтом (0xff) — легальное git-имя; ``surrogateescape``
#: возвращает те же байты при записи в ФС.
RAW_NAME = os.fsdecode(b"bad\xff.txt")
RAW_BYTES = b"bad\xff.txt"


def _publish():
    try:
        from awf import publish
    except ImportError as e:  # pragma: no cover — базовое дерево
        raise AssertionError(f"R2.5 publish module is missing: {e}") from e
    return publish


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True
    )


def _new_repo(tmp_path: Path) -> Path:
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


def _write_raw(repo: Path, name: str, content: bytes) -> None:
    with open(os.path.join(repo, name), "wb") as f:
        f.write(content)


def _head_names_raw(repo: Path) -> set[bytes]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"],
        cwd=repo, check=True, capture_output=True,
    ).stdout
    return {p for p in out.split(b"\0") if p}


def test_publish_of_non_utf8_path_is_not_an_untyped_crash(tmp_path):
    """План с байт-верным именем публикуется (не падает сырым исключением).

    План строится ШТАТНЫМ R2.3-путём (``build_commit_plan`` с baseline) —
    модель git отдаёт имя с ``surrogateescape``, отказа на такое имя нет.
    Публикация обязана дойти до типизированного исхода; регресс-инвариант
    R2.1/R2.5 — не-UTF-8 путь коммитится (пре-R2.5 ``git commit`` это
    умел), а не валит стадию ``UnicodeEncodeError`` мимо типов."""
    from awf import commit_gate, commit_plan

    repo = _new_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_raw(repo, RAW_NAME, b"content\n")

    plan = commit_plan.build_commit_plan(repo, TODO, baseline_sha=base)
    assert RAW_NAME in plan.files, (
        "sanity: the byte-faithful file name must reach the plan (R2.1 model); "
        f"got {plan.files!r}"
    )

    try:
        out = commit_gate._commit_via_isolated_index(
            repo, plan, f"awf(verify): {TODO}", repo / ".agentic" / "logs"
        )
    except Exception as e:  # noqa: BLE001 — любой строк не типизирован = дефект
        pytest.fail(
            "publishing a unit that touches a non-UTF-8 path raised an "
            f"untyped {type(e).__name__}: {e} — the publication path must "
            "leave a typed CommitOutcome (intent/receipt write is where the "
            "surrogate path breaks utf-8 JSON)"
        )

    assert out.status == "committed", (
        "R2.5 must not regress the byte-faithful path support R2.1 promises: "
        f"a non-UTF-8 path used to commit; got {out.status}: {out.reason}"
    )
    assert RAW_BYTES in _head_names_raw(repo), (
        "the unit's non-UTF-8 path did not reach HEAD"
    )
    receipt = _publish().read_receipt(repo, TODO)
    assert receipt is not None and receipt.commit_oid, (
        "a committed non-UTF-8 unit must leave a receipt with its OID"
    )


def test_commit_audit_write_is_best_effort_for_non_utf8_paths(tmp_path):
    """Best-effort писатель аудита не бросает на байт-верном пути.

    ``_write_commit_audit_record`` документирован «логирует и идёт дальше» —
    аудит это трасса, не ворота. С выходом на managed-писателя (этот юнит)
    семейство ловимых — ``(OSError, ManagedPathError)``, а ``ensure_ascii=False``
    на пути с суррогатом бросает ``UnicodeEncodeError`` (ValueError) мимо них:
    трасса аудита роняет стадию вместо деградации."""
    from awf import commit_gate

    repo = _new_repo(tmp_path)
    record = {
        "todo_id": TODO,
        "status": "committed",
        "commit": "abc1234",
        "excluded": [[RAW_NAME, "foreign entry"]],
        "worker_staged": [RAW_NAME],
        "skip_inconsistent": False,
        "skip_reason": "",
    }
    try:
        commit_gate._write_commit_audit_record(
            repo, TODO, record, repo / ".agentic" / "logs"
        )
    except Exception as e:  # noqa: BLE001 — документированный контракт: не бросать
        pytest.fail(
            "the best-effort commit-audit writer raised "
            f"{type(e).__name__}: {e} — it must log and move on, never crash "
            "the stage, for a byte-faithful (non-UTF-8) path"
        )
