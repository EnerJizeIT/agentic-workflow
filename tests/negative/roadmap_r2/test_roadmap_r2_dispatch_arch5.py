"""R2.2 architect-review, iter 9 (TODO-0221): the M4/r2.1 byte-faithful
rework missed the ``BASELINE-*.include`` link writer, and two ``tree_fingerprint``
calls in ``claim_changes`` stayed bare.

Файл КРАСНЫЙ на дереве до фикса стадии agent-implementer (REVIEW-фикс 7).

M4: ``write_include_audit`` пишет RAW-пути модели R2.1 (``display_path``:
``surrogateescape``) через строгий UTF-8. Имя с не-UTF-8 байтом приходит
одиноким суррогатом → ``UnicodeEncodeError`` ПОСЛЕ резерва TODO:

* dispatch оставляет ``TODO.md`` без ``.ready``/``.include`` (полудиспатч);
* ``update_todo`` → ``_recompute_include_baseline`` перезаписывает
  ``BASELINE-*.untracked`` ДО краха, а ссылку не пишет — на следующем
  re-baseline include исключение теряется и файл молча выпадает из коммита.

Сосед по каналу ``reject_files`` уже пишет ``errors="surrogateescape"``.

LOW-3: ``plan_claim_changes`` / ``apply_claim_changes`` зовут
``tree_fingerprint`` без ``try`` — сбой чтения дерева выпускает
``GitModelError`` (``RuntimeError``) вместо ``AwfApiError`` (сиблинг
починенного LOW-1 в ``claim_drift``).

Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

from awf import api, claim_changes, include_untracked
from awf._errors import AwfApiError

SRC = "TODO-0001"
RAW_NAME = os.fsdecode(b"\xff\xfe.txt")  # lone surrogates, exact bytes


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _project(tmp_path: Path, *, policy: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "tester")
    api.init_project(
        repo,
        project_name="R2ARCH5",
        test_cmd="",
        lint_cmd="",
        typecheck_cmd="",
        build_cmd="",
    )
    cfg = repo / ".agentic" / "config.yaml"
    data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
    data["config_version"] = 1
    data["discipline"] = {"dirty_policy": policy}
    cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "awf init")
    return repo


def _non_utf8_file(repo: Path) -> Path:
    """Create the untracked file whose name is NOT valid UTF-8."""
    fs_path = os.path.join(os.fsencode(repo), b"\xff\xfe.txt")
    with open(fs_path, "wb") as f:
        f.write(b"payload\n")
    return Path(os.fsdecode(fs_path))


def test_include_audit_roundtrips_non_utf8_under_isolate(tmp_path):
    """M4: the include link must round-trip the exact raw bytes, and the
    dispatch must reach the ``.ready`` signal (no half-dispatch)."""
    repo = _project(tmp_path, policy="isolate")
    _non_utf8_file(repo)

    result = api.dispatch_todo(
        repo,
        f"# {SRC}\norigin\n",
        todo_id=SRC,
        include_untracked=[RAW_NAME],
    )
    assert result.todo_id == SRC
    assert (repo / ".agentic" / "inbox" / f"{SRC}.ready").is_file(), (
        "dispatch stopped after reserving the TODO — no .ready signal"
    )
    link = repo / ".agentic" / "context" / f"BASELINE-{SRC}.include"
    assert link.read_bytes() == b"\xff\xfe.txt\x00", (
        f"include link is not byte-faithful: {link.read_bytes()!r}"
    )
    assert include_untracked.read_include_list(repo, SRC) == [RAW_NAME]

    from awf.api.hygiene import transfer_exclusions

    _carry, include, _claim = transfer_exclusions(repo, SRC)
    assert include == {RAW_NAME}, "re-baseline loses the non-UTF-8 include link"


def test_include_audit_non_utf8_under_require_clean(tmp_path):
    """M4 also holds on the adopted-path gate: the include is resolved BEFORE
    ``require_clean``, so a raw non-UTF-8 adopted file is not a dirty refusal
    and the dispatch completes."""
    repo = _project(tmp_path, policy="require_clean")
    _non_utf8_file(repo)

    result = api.dispatch_todo(
        repo,
        f"# {SRC}\norigin\n",
        todo_id=SRC,
        include_untracked=[RAW_NAME],
    )
    assert result.todo_id == SRC
    assert (repo / ".agentic" / "inbox" / f"{SRC}.ready").is_file()
    assert include_untracked.read_include_list(repo, SRC) == [RAW_NAME]


# ─── LOW-3: claim tree reads wrap GitModelError ──────────────────────────


def _claimable(repo: Path) -> None:
    (repo / "core.py").write_text("v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "core")
    api.create_baseline(repo, SRC)
    (repo / "core.py").write_text("predecessor work\n", encoding="utf-8")


def _boom(_root):
    from awf.infra import git_model

    raise git_model.GitModelError("git could not run (timeout or git unavailable)")


def test_plan_claim_wraps_tree_read_failure(tmp_path, monkeypatch):
    repo = _project(tmp_path, policy="isolate")
    _claimable(repo)

    from awf.infra import git_model

    monkeypatch.setattr(git_model, "tree_fingerprint", _boom)
    with pytest.raises(AwfApiError):
        claim_changes.plan_claim_changes(repo, SRC)


def test_apply_claim_wraps_tree_read_failure(tmp_path, monkeypatch):
    repo = _project(tmp_path, policy="isolate")
    _claimable(repo)
    plan = claim_changes.plan_claim_changes(repo, SRC)

    from awf.infra import git_model

    monkeypatch.setattr(git_model, "tree_fingerprint", _boom)
    with pytest.raises(AwfApiError):
        claim_changes.apply_claim_changes(repo, plan, confirmed_digest=plan.digest)
