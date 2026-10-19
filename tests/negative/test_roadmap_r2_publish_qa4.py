"""R2.5 (roadmap R2) — QA-проба границ, четвёртый блок (agent-qa-review, iter 6).

Два независимых дефекта в протоколе публикации, каждый красный на текущем
дереве (проверено прогоном) и зелёный после фикса:

1. **Intent без OID завершается ложным ``committed``.** ``read_intent``
   обещает «факт не той формы не описывает живую операцию — вызывающий идёт
   заново», но ``PublishIntent.from_dict`` принимает ``{"todo_id": ...}``:
   все поля, включая ``commit_oid``, получают значения по умолчанию. В
   репозитории с UNBORN HEAD ``_advance_publication`` пропускает update-ref
   (``if intent.commit_oid and ...``), не двигает ни одной ссылки и пишет
   receipt ``committed`` с пустым OID — ложный полный успех, который
   авторизует архив (требование 3: никакого искусственного пустого commit).
   Инвариант: ``committed`` receipt обязан нести OID, достижимый из HEAD.

2. **Resume после crash на index-sync оставляет пользовательский index
   позади уехавшего HEAD.** ``_sync_owned_index`` считает запись «уже
   синхронизированной», если она равна версии СВОЕГО коммита — но HEAD с
   тех пор ушёл на потомка (D1), и файл в HEAD другой. Запись остаётся на
   версии предка: ``git diff --cached`` показывает фантомный staged-revert,
   а receipt рапортует ``index_sync: synced``. Требование 2: owned-запись
   (она доказанно наша) синхронизируется с HEAD, а не с промежуточным
   коммитом операции.

Оба теста — на disposable-репозиториях в ``tmp_path``; реальный git не
трогается, прод-код не правится (находки уходят супервизору).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

TODO = "TODO-0001"


def _publish():
    try:
        from awf import publish
    except ImportError as e:  # pragma: no cover — базовое дерево
        raise AssertionError(f"R2.5 publish module is missing: {e}") from e
    return publish


def _git(repo: Path, *args: str, check: bool = True, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _new_repo(tmp_path: Path, *, commit: bool) -> Path:
    from awf import paths

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")
    if commit:
        (repo / "README.md").write_text("v1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "init")
    logs = repo / ".agentic" / "logs"
    logs.mkdir(parents=True)
    paths.context_dir(repo).mkdir(parents=True, exist_ok=True)
    return repo


def _logs(repo: Path) -> Path:
    return repo / ".agentic" / "logs"


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _count(repo: Path) -> int:
    return int(_git(repo, "rev-list", "--count", "HEAD").stdout.strip())


def _resume(repo: Path, base: str = ""):
    from awf import commit_gate

    return commit_gate.maybe_commit(
        "verify", TODO, "commit_and_report", repo, _logs(repo), auto=False,
        baseline_sha=base,
    )


def _commit(repo: Path, base: str = "", crash_at: str = ""):
    from awf import commit_gate, commit_plan

    plan = commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=("README.md",),
        baseline_sha=base,
    )
    return commit_gate._commit_via_isolated_index(
        repo, plan, f"awf(verify): {TODO}", _logs(repo), crash_at=crash_at
    )


def test_intent_without_commit_oid_is_not_completed_as_committed(tmp_path):
    """A pending-intent fact with no OID cannot describe a live operation.

    On an UNBORN HEAD the current code skips the ref move and writes a
    ``committed`` receipt with ``commit_oid == ""`` — a false full success
    (no commit exists at all) that authorizes the archive. Whatever the fix
    does with the malformed fact (ignore → run fresh, or refuse), a
    ``committed`` outcome must mean the work is actually in HEAD."""
    publish = _publish()
    repo = _new_repo(tmp_path, commit=False)  # unborn HEAD is supported
    (repo / "unit.txt").write_text("u\n", encoding="utf-8")
    publish.intent_file(repo, TODO).write_text(
        json.dumps({"todo_id": TODO}) + "\n", encoding="utf-8"
    )

    out = _resume(repo)

    receipt = publish.read_receipt(repo, TODO)
    has_head = _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode == 0
    if out.status == "committed" or (
        receipt is not None
        and receipt.disposition
        in (publish.DISPOSITION_COMMITTED, publish.DISPOSITION_RECONCILIATION)
    ):
        assert out.sha, (
            "a committed outcome was reported with an empty commit sha"
        )
        assert receipt is not None and receipt.commit_oid, (
            "a committed receipt was written with an empty commit oid"
        )
        assert has_head, "a committed outcome was reported but HEAD does not exist"
        assert _git(
            repo, "merge-base", "--is-ancestor", receipt.commit_oid, "HEAD", check=False
        ).returncode == 0, "the receipt's commit is not reachable from HEAD"


def test_resume_after_index_sync_keeps_index_in_step_with_moved_head(tmp_path):
    """Crash AFTER the owned-index sync, then HEAD moves to a descendant.

    The real index still holds the unit's own committed entry (the crash
    skipped nothing — the sync already ran). A later actor (a separate
    index, so the user's index is untouched) commits a NEWER version of the
    same file on top. Resume finishes the receipt (D1) but must leave the
    user's index in step with HEAD: the entry is proven ours, so resetting
    it to HEAD cannot clobber foreign work. The current code treats "equals
    our commit" as "already synced" and leaves a phantom staged revert
    behind while the receipt says ``index_sync: synced``."""
    publish = _publish()
    repo = _new_repo(tmp_path, commit=True)
    base = _head(repo)
    (repo / "README.md").write_text("v2\n", encoding="utf-8")

    with pytest.raises(publish.PublishCrash):
        _commit(repo, base=base, crash_at="index_sync")
    unit_oid = publish.read_intent(repo, TODO).commit_oid
    assert _head(repo) == unit_oid
    assert _git(repo, "ls-files", "-s", "README.md").stdout.startswith("100644 ")

    # A later actor commits README v3 through a SEPARATE index: HEAD moves
    # to a descendant, the user's real index is left at the unit's version.
    (repo / "README.md").write_text("v3\n", encoding="utf-8")
    later_index = tmp_path / "later.idx"
    env = {**os.environ, "GIT_INDEX_FILE": str(later_index)}
    _git(repo, "read-tree", "HEAD", env=env)
    _git(repo, "add", "README.md", env=env)
    tree = _git(repo, "write-tree", env=env).stdout.strip()
    later = _git(
        repo, "commit-tree", tree, "-p", "HEAD", "-m", "later actor"
    ).stdout.strip()
    _git(repo, "update-ref", "HEAD", later)
    count = _count(repo)

    out = _resume(repo)
    assert out.status == "committed", out.reason
    assert _count(repo) == count, "resume added a commit"
    assert _head(repo) == later, "resume moved HEAD"

    receipt = publish.read_receipt(repo, TODO)
    assert receipt is not None and receipt.commit_oid == unit_oid
    staged = _git(repo, "diff", "--cached", "--quiet", "--", "README.md", check=False)
    assert staged.returncode == 0, (
        "the user's index still holds the unit's (ancestor) version after the "
        "resume: `git diff --cached` shows a phantom staged revert although "
        "the receipt claims index_sync=synced"
    )
