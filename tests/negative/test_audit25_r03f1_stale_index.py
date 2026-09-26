"""R-03-F1 (TODO-0093, живой инцидент 26.09): изолированный коммит не
должен оставлять «застывший» пользовательский index.

Дефект: коммит идёт через одноразовый ``GIT_INDEX_FILE`` — реальный index
после него остаётся на прошлой базе. Для файлов плана записи index не
сходятся с новым HEAD, и ``git diff --cached --name-only <новый HEAD>``
выдаёт их как «чужой staged». Последствия:
1. следующий юнит (база = новый HEAD) вычитает свои же закоммиченные
   файлы из плана как «foreign» — файл молча не попадает в коммит
   (инцидент A-17: ``awf/metrics.py`` потерян, история восстанавливалась
   вручную);
2. ``git status`` показывает закоммиченное как staged/удалённое —
   исполнители пытаются «починить» его git-командами.

Инварианты:
1. После успешного изолированного коммита записи файлов плана в реальном
   index совпадают с новым HEAD; чужие staged записи вне плана не
   трогаются.
2. Следующий юнит с базой на новом HEAD коммитит повторно изменённый файл
   (не вычитается как «чужой»).
3. Защита чужих staged сохраняется (A-01): чужой staged не попадает в
   коммит и не снимается.

До фикса красные: ``test_second_unit_commit_keeps_touched_file`` (файл
вычитается из плана юнита 2 как «foreign» → skipped) и
``test_isolated_commit_syncs_real_index_for_plan`` (index застыл на
прошлой базе).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from awf import commit_plan
from awf.commit_gate import _commit_via_isolated_index, maybe_commit

TODO = "TODO-0093"


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _status(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()


def _staged_vs_head(repo: Path, ref: str) -> list[str]:
    """What the real index has staged against ``ref`` (paths, one per line)."""
    return subprocess.run(
        ["git", "diff", "--cached", "--name-only", ref],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()


def _committed_files(repo: Path) -> list[str]:
    return subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.splitlines()


def _index_entries(repo: Path) -> str:
    """The user's index itself (paths + blob SHAs) — not relative to HEAD."""
    return subprocess.run(
        ["git", "ls-files", "--stage"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _plan(files: tuple[str, ...]) -> commit_plan.CommitPlan:
    return commit_plan.CommitPlan(
        todo_id=TODO, generation=0, verified_sha="", files=files
    )


def _logs(repo: Path) -> Path:
    # Outside the repo: the gate's own orchestrator.log (written after
    # the commit) must not land in the NEXT unit's plan as untracked.
    logs = repo.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs


def test_second_unit_commit_keeps_touched_file(tmp_git_repo: Path):
    """Инвариант 2 (инцидент A-17): юнит 1 коммитит файл; юнит 2 (база =
    HEAD после юнита 1) меняет ТОТ ЖЕ файл — файл обязан попасть в коммит
    юнита 2. До фикса: записи застывшего index не сходятся с новым HEAD,
    план юнита 2 вычитает файл как «foreign» → коммит skipped."""
    repo = tmp_git_repo
    logs = _logs(repo)

    # юнит 1: рабочая правка файла, коммит через гейт
    base1 = _head(repo)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    outcome1 = maybe_commit(
        "verify", TODO, "commit_and_next", repo, logs,
        auto=False, baseline_sha=base1,
    )
    assert outcome1.status == "committed", (
        f"unit 1 must commit — got {outcome1.status}: {outcome1.reason}"
    )
    head1 = _head(repo)
    assert head1 != base1

    # юнит 2: новая база = HEAD после юнита 1, тот же файл
    (repo / "README.md").write_text("v2\n", encoding="utf-8")
    outcome2 = maybe_commit(
        "verify", TODO, "commit_and_next", repo, logs,
        auto=False, baseline_sha=head1,
    )

    assert outcome2.status == "committed", (
        "R-03-F1: the second unit's own file was subtracted from the plan "
        f"as 'foreign' by the stale index — got {outcome2.status}: {outcome2.reason}"
    )
    assert _committed_files(repo) == ["README.md"], (
        "the second unit's commit must contain the touched file"
    )


def test_isolated_commit_syncs_real_index_for_plan(tmp_git_repo: Path):
    """Инвариант 1: после успешного изолированного коммита ``git diff
    --cached --name-only HEAD`` пуст для файлов плана — записи index
    переставлены на новый HEAD; чужой staged файл остался staged. До
    фикса: index застыл на прошлой базе — файл плана виден как staged."""
    repo = tmp_git_repo

    (repo / "foreign.txt").write_text("user WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")

    outcome = _commit_via_isolated_index(
        repo, _plan(("README.md",)), f"awf(verify): {TODO}"
    )

    assert outcome.status == "committed", (
        f"the unit commit must proceed — got {outcome.status}: {outcome.reason}"
    )
    staged = _staged_vs_head(repo, "HEAD")
    assert "README.md" not in staged, (
        "R-03-F1: after the unit commit the plan's files must match HEAD "
        f"in the real index — the stale index still shows: {staged}"
    )
    assert staged == ["foreign.txt"], (
        "only the foreign staged entry may remain staged vs HEAD"
    )
    assert "A  foreign.txt" in _status(repo), (
        "the foreign staged file must stay staged"
    )
    assert (repo / "README.md").read_text(encoding="utf-8") == "v1\n", (
        "the sync must not touch the working tree"
    )


def test_foreign_staged_stays_out_of_commit_and_staged(tmp_git_repo: Path):
    """Инвариант 3 (регрессия A-01): чужой staged до вызова не попадает в
    коммит и не снимается — ни до, ни после синхронизации index."""
    repo = tmp_git_repo

    (repo / "foreign.txt").write_text("user WIP\n", encoding="utf-8")
    subprocess.run(["git", "add", "foreign.txt"], cwd=repo, check=True)
    (repo / "README.md").write_text("v1\n", encoding="utf-8")
    foreign_before = [
        line for line in _index_entries(repo).splitlines() if "foreign.txt" in line
    ]

    outcome = _commit_via_isolated_index(
        repo, _plan(("README.md",)), f"awf(verify): {TODO}"
    )

    assert outcome.status == "committed"
    assert _committed_files(repo) == ["README.md"], (
        "the commit must contain exactly the plan's files — the foreign "
        "entry may not leak in"
    )
    assert "A  foreign.txt" in _status(repo), (
        "the user's foreign file must stay staged exactly as before"
    )
    foreign_after = [
        line for line in _index_entries(repo).splitlines() if "foreign.txt" in line
    ]
    assert foreign_after == foreign_before, (
        "A-01: the foreign staged entry must be untouched by the commit "
        "and the index sync"
    )
