"""R2.3 (roadmap R2): CandidatePlan + DeltaAssembler — приёмочная матрица.

Проверяет НОВЫЙ единый сборщик кандидата (``awf.commit_delta``):
CandidatePlan-поля, типизированные операции (rename-пара, symlink, binary,
typechange), явный отказ на смешанной паре владения, и property-матрицу
``include_untracked × pre-existing untracked × carry-over × staged rename``
с точным оракулом.

Строки приёмочной матрицы R2, закрываемые здесь:
  S2-01 — чужой staged rename не удаляет source из HEAD и не включает
          destination (пара исключается целиком);
  B01   — собственный rename включает destination И deletion source;
  Матрица — property-генератор с сидом и точным оракулом;
  Неразмещаемое — смешанная пара/неподдержанный случай = отказ, не silent
          legacy.

Ленивый импорт: на базовом дереве ``awf.commit_delta`` отсутствует —
провал должен быть утверждением («модуля нет»), а не import-ошибкой
(broken-runner).
"""

from __future__ import annotations

import os
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

T = "TODO-0001"


def _delta():
    """Ленивый импорт: на базовом дереве модуля нет."""
    try:
        from awf import commit_delta
    except ImportError as e:  # pragma: no cover - baseline only
        raise AssertionError(f"awf.commit_delta module is missing: {e}") from e
    return commit_delta


REPO_ROOT = Path(__file__).resolve().parents[3]


def _git(proj: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=proj, check=check, capture_output=True, text=True
    )


def _write(proj: Path, path: str, data: str | bytes) -> None:
    p = proj / path
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        p.write_bytes(data)
    else:
        p.write_text(data, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    proj = tmp_path / "repo"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t.t")
    _git(proj, "config", "user.name", "tester")
    (proj / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
    # the AWF runtime dir is ignored (as init_project arranges), so the
    # baseline files never leak into the untracked set.
    _write(proj, ".gitignore", ".agentic/\n")
    return proj


def _commit_baseline_files(proj: Path, todo: str = T) -> str:
    """Write the AWF baseline files the way ``create_baseline`` does, and
    return the baseline sha. ``.include`` is NOT written (the assembler's
    include channel is exercised by dropping the path from ``.untracked``,
    exactly as the real dispatch does)."""
    sha = _git(proj, "rev-parse", "HEAD").stdout.strip()
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{todo}.sha").write_text(sha + "\n")
    status = _git(proj, "status", "--short", check=False).stdout
    (ctx / f"BASELINE-{todo}.status").write_text(status)
    return sha


def _snapshot_with_worktree(proj: Path, todo: str = T) -> str:
    """Write the AWF baseline with the dispatch-time WORKTREE snapshot — the
    precondition the REPORTS33 delta mechanism needs (``.status`` + ``.worktree``)."""
    sha = _git(proj, "rev-parse", "HEAD").stdout.strip()
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{todo}.sha").write_text(sha + "\n")
    (ctx / f"BASELINE-{todo}.status").write_text(
        _git(proj, "status", "--short", check=False).stdout
    )
    (ctx / f"BASELINE-{todo}.untracked").write_text(
        _git(proj, "ls-files", "--others", "--exclude-standard").stdout
    )
    snap = _git(proj, "stash", "create").stdout.strip()
    (ctx / f"BASELINE-{todo}.worktree").write_text(snap + "\n")
    return sha


def _rebaseline(
    proj: Path, todo: str, *, drop_from_untracked: tuple[str, ...] = ()
) -> str:
    """Snapshot the baseline now, dropping adopted (include/carry-over) paths
    from ``.untracked`` — the mechanism ``create_baseline`` uses."""
    sha = _git(proj, "rev-parse", "HEAD").stdout.strip()
    ctx = proj / ".agentic" / "context"
    (ctx / f"BASELINE-{todo}.sha").write_text(sha + "\n")
    status = _git(proj, "status", "--short", check=False).stdout
    (ctx / f"BASELINE-{todo}.status").write_text(status)
    untracked = [
        p
        for p in _git(proj, "ls-files", "--others", "--exclude-standard")
        .stdout.splitlines()
        if p and p not in set(drop_from_untracked)
    ]
    (ctx / f"BASELINE-{todo}.untracked").write_text(
        "\n".join(untracked) + ("\n" if untracked else "")
    )
    return sha


# ─── CandidatePlan: поля и типизированные операции ───────────────────────


def test_candidate_plan_records_fields(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _commit_baseline_files(proj)
    _write(proj, "a.txt", "A2\n")
    _write(proj, "new.txt", "N\n")

    plan = delta.assemble_candidate(proj, T, baseline, build_tree=True)

    assert plan.todo_id == T
    assert plan.head_expected == _git(proj, "rev-parse", "HEAD").stdout.strip()
    assert len(plan.baseline_digest) == 64
    assert "a.txt" in plan.files and "new.txt" in plan.files
    assert plan.candidate_tree, "candidate tree oid must be materialised"
    kinds = {op.raw_path: op.kind for op in plan.operations}
    assert kinds.get("a.txt") == "modified"
    assert kinds.get("new.txt") == "untracked"


def test_symlink_operation_keeps_the_link_not_target_bytes(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "target_one.txt", "ONE\n")
    _write(proj, "target_two.txt", "TWO\n")
    os.symlink("target_one.txt", proj / "link")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _commit_baseline_files(proj)
    # unit repoints the link (staged, so the index carries the new link blob)
    (proj / "link").unlink()
    os.symlink("target_two.txt", proj / "link")
    _git(proj, "add", "link")

    plan = delta.assemble_candidate(proj, T, baseline)
    ops = [op for op in plan.operations if op.raw_path == "link"]
    assert ops, "the symlink change must be a typed operation"
    op = ops[0]
    assert op.is_symlink and op.mode == "120000"
    # the blob is the LINK TARGET STRING, never the bytes behind it
    want = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=proj,
        input=b"target_two.txt",
        capture_output=True,
    ).stdout.decode().strip()
    assert op.blob == want


def test_claim_link_channel_enters_the_commit_plan(tmp_path: Path) -> None:
    """REVIEW-F1: the PROD call must propagate the CODE-claim channel
    (``BASELINE-<todo>.claim`` link + ``CLAIM-<origin>.json``, the
    ``transfer_exclusions`` mechanism of TODO-0221) into the DeltaAssembler —
    a claimed tracked predecessor change is committed WHOLE, and without the
    link it honestly stays out."""
    _delta()
    from awf import commit_plan
    from awf.claim_changes import (
        apply_claim_changes,
        plan_claim_changes,
        write_claim_link,
    )

    proj = _repo(tmp_path)
    _write(proj, "f.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    ctx = proj / ".agentic" / "context"
    (ctx / "BASELINE-TODO-0002.sha").write_text(
        _git(proj, "rev-parse", "HEAD").stdout.strip() + "\n"
    )
    _write(proj, "f.txt", "A foreign\n")  # pre-dispatch predecessor edit
    claim = plan_claim_changes(proj, "TODO-0002", paths=["f.txt"])
    apply_claim_changes(proj, claim, confirmed_digest=claim.digest)
    baseline = _snapshot_with_worktree(proj)
    _write(proj, "unit.txt", "U\n")  # the unit does not touch f.txt

    # Without the audit link the claimed path is pre-existing — out of the plan.
    without = commit_plan._plan_split(proj, baseline, T)
    assert "f.txt" not in without.files, "без claim-линка чужое изменение не входит"

    write_claim_link(proj, T, "TODO-0002")
    with_link = commit_plan._plan_split(proj, baseline, T)
    assert "f.txt" in with_link.files, "claim-канал обязан войти в commit-план"
    assert "unit.txt" in with_link.files


def test_claimed_file_is_committed_whole_not_delta_merged(tmp_path: Path) -> None:
    """REVIEW-F1: a claimed tracked file the unit ALSO edits must carry the
    FULL worktree content (predecessor blob + unit edit). The per-file delta
    keeps only the unit's own edit and drops the predecessor's change — the
    very blob the claim consented to."""
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "f.txt", "a\nb\nc\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, "f.txt", "P\nb\nc\n")  # predecessor edits line 1
    baseline = _snapshot_with_worktree(proj)
    _write(proj, "f.txt", "P\nU\nc\n")  # the unit edits line 2 of the same file

    plan = delta.assemble_candidate(
        proj, T, baseline, claim=["f.txt"], build_tree=True
    )

    assert "f.txt" in plan.files
    assert "f.txt" not in plan.delta_files, "claim = полный файл, не delta-merge"
    assert (
        _git(proj, "cat-file", "-p", f"{plan.candidate_tree}:f.txt").stdout
        == "P\nU\nc\n"
    ), "предшественник (строка 1) не должен потеряться"


# ─── S2-01: чужой staged rename — пара исключается целиком ────────────────


def test_foreign_staged_rename_is_excluded_whole(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    # foreign staged rename BEFORE dispatch
    _git(proj, "mv", "keep.txt", "moved.txt")
    baseline = _commit_baseline_files(proj)

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "moved.txt" not in plan.files, (
        "чужой rename: destination не должен попасть в коммит"
    )
    # source must remain in HEAD — the unit commit does not touch it
    assert _git(proj, "cat-file", "-e", "HEAD:keep.txt", check=False).returncode == 0
    # neither half is even a conflict (both foreign)
    assert not plan.conflicts


# ─── B01: собственный rename включает destination + deletion ──────────────


def test_own_rename_includes_destination_and_deletion(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    baseline = _commit_baseline_files(proj)
    # unit renames after dispatch
    _git(proj, "mv", "a.txt", "b.txt")

    plan = delta.assemble_candidate(proj, T, baseline)

    assert "b.txt" in plan.files, "destination обязан войти в коммит"
    assert "a.txt" in plan.files, (
        "source обязан войти как deletion — иначе старый файл останется"
    )


# ─── Смешанная пара владения — явный отказ ───────────────────────────────


def test_mixed_ownership_pair_is_an_explicit_conflict(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    # pre-dispatch: the SOURCE was already modified (foreign)
    _write(proj, "keep.txt", "K foreign\n")
    baseline = _commit_baseline_files(proj)
    # the unit renames the pre-dirty source -> mixed pair
    _git(proj, "mv", "keep.txt", "moved.txt")

    plan = delta.assemble_candidate(proj, T, baseline)
    assert plan.conflicts, "смешанная пара обязана стать конфликтом"
    assert "moved.txt" not in plan.files and "keep.txt" not in plan.files

    with pytest.raises(delta.DeltaError):
        delta.assemble_candidate(proj, T, baseline, strict=True)


def test_mixed_pair_blocks_the_commit_plan(tmp_path: Path) -> None:
    """R2.3: смешанная пара должна блокировать commit-план (refusal), а не
    молча выкидываться из набора — иначе гейт закоммитит остаток, потеряв
    пару без отказа."""
    _delta()
    from awf import commit_plan

    proj = _repo(tmp_path)
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _write(proj, "keep.txt", "K foreign\n")  # pre-dispatch: source foreign
    baseline = _commit_baseline_files(proj)
    _git(proj, "mv", "keep.txt", "moved.txt")  # unit renames pre-dirty source

    split = commit_plan._plan_split(proj, baseline, T)
    assert split.files == [], "смешанная пара блокирует набор"
    assert split.refusal, "смешанная пара обязана дать явный отказ"

    plan = commit_plan.build_commit_plan(proj, T, baseline_sha=baseline)
    assert plan.refusal, "гейт обязан увидеть отказ, а не коммитить остаток"


# ─── Неподдержанное — отказ, не silent legacy ────────────────────────────


def test_gitlink_change_is_an_explicit_refusal(tmp_path: Path) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    # a gitlink (submodule) entry — unsupported by the model
    _git(
        proj,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{_git(proj, 'rev-parse', 'HEAD').stdout.strip()},sub",
    )
    baseline = _commit_baseline_files(proj)

    with pytest.raises(delta.GitModelError):
        delta.assemble_candidate(proj, T, baseline)


def test_plan_split_surfaces_the_refusal(tmp_path: Path) -> None:
    _delta()
    from awf import commit_plan

    proj = _repo(tmp_path)
    _write(proj, "a.txt", "A\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    _git(
        proj,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{_git(proj, 'rev-parse', 'HEAD').stdout.strip()},sub",
    )
    baseline = _commit_baseline_files(proj)

    split = commit_plan._plan_split(proj, baseline, T)
    assert split.files == []
    assert split.refusal, "неподдержанный случай обязан дать явный отказ"


# ─── property-матрица: include × pre-existing × carry-over × rename ──────

SEED = 20261018
N_CASES = 32


@dataclass(frozen=True)
class Case:
    include_untracked: bool
    pre_existing_untracked: bool
    carry_over: bool
    rename: str  # "none" | "foreign" | "own"


def _gen(seed: int, n: int) -> list[Case]:
    rng = random.Random(seed)
    out: list[Case] = []
    for _ in range(n):
        out.append(
            Case(
                include_untracked=rng.random() < 0.5,
                pre_existing_untracked=rng.random() < 0.5,
                carry_over=rng.random() < 0.5,
                rename=rng.choice(["none", "foreign", "own"]),
            )
        )
    # guarantee every orthogonal combination is exercised at least once
    for inc in (False, True):
        for pre in (False, True):
            for co in (False, True):
                for rn in ("none", "foreign", "own"):
                    out.append(Case(inc, pre, co, rn))
    return out


def _oracle(case: Case) -> set[str]:
    """Точный оракул: какие пути обязан содержать собранный кандидат."""
    files: set[str] = {"unit_new.txt"}
    if case.include_untracked:
        files.add("up_new.txt")
    if case.carry_over:
        files.add("co.txt")
    if case.rename == "own":
        files.add("a.txt")  # deletion of the source
        files.add("b.txt")
    return files


def _build(proj: Path, case: Case) -> str:
    _write(proj, "a.txt", "A\n")
    _write(proj, "keep.txt", "K\n")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "init")
    drop: list[str] = []
    if case.include_untracked:
        _write(proj, "up_new.txt", "U\n")
        drop.append("up_new.txt")
    if case.carry_over:
        _write(proj, "co.txt", "C\n")
        drop.append("co.txt")
    if case.pre_existing_untracked:
        _write(proj, "pre.txt", "P\n")
    if case.rename == "foreign":
        _git(proj, "mv", "keep.txt", "moved.txt")
    baseline = _rebaseline(proj, T, drop_from_untracked=tuple(drop))
    # unit work after dispatch
    if case.rename == "own":
        _git(proj, "mv", "a.txt", "b.txt")
    _write(proj, "unit_new.txt", "N\n")
    return baseline


def test_property_seed_determinism() -> None:
    assert _gen(SEED, N_CASES) == _gen(SEED, N_CASES)
    assert _gen(SEED, N_CASES) != _gen(SEED + 1, N_CASES)


@pytest.mark.parametrize("case", _gen(SEED, N_CASES), ids=lambda c: repr(c))
def test_property_matrix(tmp_path: Path, case: Case) -> None:
    delta = _delta()
    proj = _repo(tmp_path)
    baseline = _build(proj, case)

    plan = delta.assemble_candidate(proj, T, baseline)

    got = set(plan.files)
    want = _oracle(case)
    assert got == want, (
        f"{case}: кандидат {sorted(got)} ≠ оракул {sorted(want)}"
    )
    # a foreign rename must never leak either half
    assert "moved.txt" not in got
