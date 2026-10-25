"""R2.2 QA, iter 4 (TODO-0221): the carry-over channel is NOT byte-faithful.

Файл КРАСНЫЙ на текущем дереве (находка стадии agent-qa-review, iter 4).

R2.2 перевело все поверхности «грязного дерева» на модель R2.1 (raw-байты
через ``display_path``): ``include_untracked``, ``claim_from`` и сам
``check_source_tree`` сравнивают пути в RAW-форме. Осталась ОДНА ветка,
не переведённая — канал переноса finding (R5 leak-gate):

* ``reject_files.snapshot_rejected_files`` пишет ``REJECT-<origin>.files``
  через ПЕЧАТНЫЙ ``git ls-files --others --exclude-standard`` (без ``-z``),
  а git C-квотит не-ASCII имя (``"\\321\\204..."``);
* ``read_reject_files`` / ``resolve_carry_over`` возвращают эти же
  C-квоченные строки;
* а R2.2 сравнивает их с RAW-путями: adopted-set в ``dispatch_todo``,
  фильтр ``_dirty_tree_classes`` и ``pipeline._untracked_snapshot``
  (``display_path(p) not in excluded``).

Для имени с кириллицей форма не совпадает, поэтому:

1. carry_over-путь НЕ исключается из ``BASELINE-*.untracked`` (в отличие от
   include/claim — они raw и матчатся), и его «съест» commit-gate как
   pre-existing;
2. под ``require_clean`` он не попадает в adopted-set → retry получает отказ
   «source tree is dirty», хотя владелец явно переносит этот finding —
   ровно то, что чинил F2.

Прод-код стадией QA не правится (правило итерации); фикс — следующим
проходом через REVIEW. Все репозитории — disposable в ``tmp_path``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from awf import api, paths, reject_files

SRC = "TODO-0001"
RETRY = "TODO-0002"
FINDING = "файлу.txt"  # non-ASCII untracked finding of the rejected attempt


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
        project_name="R2QA4",
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


def _reject_attempt_with_finding(repo: Path) -> str:
    """Origin dispatch, then a rejected non-ASCII untracked finding recorded by
    the real leak-gate snapshot; returns the file as the leak-gate wrote it."""
    api.dispatch_todo(repo, f"# {SRC}\norigin\n", todo_id=SRC)
    (repo / FINDING).write_text("finding\n", encoding="utf-8")
    recorded = reject_files.snapshot_rejected_files(
        repo, SRC, paths.logs_dir(repo)
    )
    assert recorded, "the leak-gate must record the new untracked finding"
    return recorded[0]


def test_carry_over_non_ascii_finding_is_excluded_from_baseline_snapshot(tmp_path):
    """A carried-over non-ASCII finding must be OWNED by the retry: it drops
    from ``BASELINE-*.untracked`` exactly like an include/claim path — the
    snapshot is empty when the adopted file is the only pre-existing dirt."""
    repo = _project(tmp_path, policy="isolate")
    _reject_attempt_with_finding(repo)

    api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC
    )
    snapshot = (
        repo / ".agentic" / "context" / f"BASELINE-{RETRY}.untracked"
    ).read_text(encoding="utf-8")
    assert snapshot.strip() == "", (
        "carried-over non-ASCII finding is NOT excluded from the snapshot "
        f"(printed/raw byte-form mismatch); snapshot={snapshot!r}"
    )


def test_carry_over_non_ascii_finding_adopted_under_require_clean(tmp_path):
    """F2 holds byte-faithfully: a retry that carries over a non-ASCII
    finding must NOT be refused by ``require_clean`` — the adopted path is the
    unit's own work (the include/claim channels already do this)."""
    repo = _project(tmp_path, policy="require_clean")
    _reject_attempt_with_finding(repo)

    result = api.dispatch_todo(
        repo, f"# {RETRY}\nretry\n", todo_id=RETRY, carry_over_from=SRC
    )
    assert result.todo_id == RETRY
    assert (repo / ".agentic" / "context" / f"BASELINE-{RETRY}.sha").is_file()
