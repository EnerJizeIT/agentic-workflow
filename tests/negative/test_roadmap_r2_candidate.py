"""R2.4 (roadmap R2): проверить именно candidate и связать approval.

Строки приёмочной матрицы R2, закрываемые здесь:
  9  — рабочие тесты green, candidate red из-за исключённой зависимости:
       approve ЗАБЛОКИРОВАН с объяснением plan/excluded dependency;
  invalidation — изменился blob/mode/path/контракт после approval →
       evidence инвалидирован, commit не проходит;
  HEAD — HEAD сдвинулся → перепланирование, не commit поверх;
  degrade — неразмещаемый случай даёт degrade-маркер плана; approve без
       подтверждения деградации отвергается, с подтверждением — полный
       файл коммитится.

Проверяется НОВЫЙ модуль ``awf.candidate_verify`` и привязка в
``commit_plan``/``commit_gate``/``api.pipeline``. Ленивый импорт: на
базовом дереве модуля нет — провал должен быть утверждением, а не
import-ошибкой (broken-runner).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from awf import api, git_utils
from awf.commit_gate import maybe_commit

T = "TODO-0001"


def _cv():
    try:
        from awf import candidate_verify
    except ImportError as e:  # pragma: no cover - baseline only
        raise AssertionError(f"awf.candidate_verify module is missing: {e}") from e
    return candidate_verify


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


def _head(proj: Path) -> str:
    return _git(proj, "rev-parse", "HEAD").stdout.strip()


def _commit_all(proj: Path, message: str) -> None:
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", message)


def _repo(tmp_path: Path) -> Path:
    proj = tmp_path / "repo"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "config", "user.email", "t@t.t")
    _git(proj, "config", "user.name", "tester")
    (proj / ".agentic" / "context").mkdir(parents=True, exist_ok=True)
    _write(proj, ".gitignore", ".agentic/\n")
    return proj


def _ctx(proj: Path, suffix: str) -> Path:
    return proj / ".agentic" / "context" / f"BASELINE-{T}.{suffix}"


def _snapshot(proj: Path, sha: str) -> None:
    """Write the AWF baseline exactly as ``create_baseline`` does."""
    _ctx(proj, "sha").write_text(sha + "\n", encoding="utf-8")
    _ctx(proj, "status").write_text(
        _git(proj, "status", "--short", check=False).stdout, encoding="utf-8"
    )
    _ctx(proj, "untracked").write_text(
        _git(proj, "ls-files", "--others", "--exclude-standard").stdout,
        encoding="utf-8",
    )
    snap = _git(proj, "stash", "create", check=False).stdout.strip()
    _ctx(proj, "worktree").write_text(snap + "\n", encoding="utf-8")


def _write_todo(proj: Path, verify: list[str] | None = None) -> None:
    contract = ""
    if verify:
        contract = "---\nverify:\n" + "".join(f"  - {v}\n" for v in verify) + "---\n\n"
    inbox = proj / ".agentic" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / f"{T}.md").write_text(contract + "# Task\n", encoding="utf-8")


# ─── materialization / isolation ─────────────────────────────────────────


def test_candidate_materialized_in_isolated_worktree_user_index_untouched(tmp_path):
    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")  # the unit's edit

    index_before = (proj / ".git" / "index").read_bytes()
    m = cv.materialize_candidate(proj, T, sha, worktree_dir=tmp_path / "wt")
    index_after = (proj / ".git" / "index").read_bytes()

    assert m.refusal == "", m.refusal
    assert m.candidate_tree, "the candidate tree OID must be materialised"
    assert m.worktree is not None
    assert (m.worktree / "unit.txt").read_text() == "unit work\n"
    assert index_before == index_after, (
        "materialising the candidate must never touch the user's .git/index"
    )


# ─── row 9: excluded dependency blocks the approval ──────────────────────


def _excluded_dependency_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo whose candidate EXCLUDES a pre-existing untracked dependency
    (``dep.txt``) the unit's check needs."""
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write(proj, "dep.txt", "DEP\n")  # foreign untracked, pre-existing
    _snapshot(proj, sha)  # records dep.txt in .untracked
    _write(proj, "unit.txt", "unit work\n")
    return proj, sha


def test_missing_excluded_dependency_refuses_evidence(tmp_path):
    cv = _cv()
    proj, sha = _excluded_dependency_repo(tmp_path)
    # The check passes in the ORIGINAL dirty worktree (the dependency is there)…
    status_dirty, _ = cv.run_check(["cat dep.txt"], proj)
    assert status_dirty == cv.CHECK_OK, "the dirty worktree check must pass"
    # …but fails on the CANDIDATE (dep.txt is excluded by design).
    built = cv.build_evidence(proj, T, sha, commands=["cat dep.txt"])
    assert built.evidence is None
    assert built.refusal, "a failing candidate check must refuse the evidence"
    assert "dep.txt" in built.refusal
    assert "EXCLUDED" in built.refusal
    assert built.materialization is not None
    assert ("dep.txt", "pre-existing untracked") in built.materialization.plan.excluded


def test_excluded_dependency_blocks_run_approve(tmp_git_repo):
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandBlock")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["cat dep.txt"])
    _write(proj, "dep.txt", "DEP\n")
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")

    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    with pytest.raises(Exception) as exc:
        api.approve_commit(proj, T, evidence="x", verified_sha=fp)
    assert "candidate" in str(exc.value).lower()
    assert not (proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready").exists(), (
        "no APPROVE signal may be published on a refused candidate"
    )


# ─── invalidation: blob / mode / contract / HEAD ─────────────────────────


def _evidence_repo(tmp_path: Path, *, verify: list[str] | None = None):
    """A repo with a built, valid evidence for the unit's edit."""
    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=verify)
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    built = cv.build_evidence(proj, T, sha)
    assert built.evidence is not None, built.refusal
    return cv, proj, sha, built.evidence


def test_blob_change_after_approval_invalidates_evidence(tmp_path):
    cv, proj, sha, ev = _evidence_repo(tmp_path)
    _write(proj, "unit.txt", "TAMPERED\n")
    refusal = cv.validate_evidence(ev, proj, T, sha)
    assert refusal and "candidate content changed" in refusal.lower()


def test_mode_change_after_approval_invalidates_evidence(tmp_path):
    cv, proj, sha, ev = _evidence_repo(tmp_path)
    (proj / "unit.txt").chmod(0o755)  # 100644 -> 100755
    refusal = cv.validate_evidence(ev, proj, T, sha)
    assert refusal and "candidate content changed" in refusal.lower()


def test_contract_change_after_approval_invalidates_evidence(tmp_path):
    cv, proj, sha, ev = _evidence_repo(tmp_path, verify=["echo ok"])
    _write_todo(proj, verify=["echo b"])  # the contract moves
    refusal = cv.validate_evidence(ev, proj, T, sha)
    assert refusal and "contract changed" in refusal.lower()


def test_head_moved_after_approval_requires_replan(tmp_path):
    cv, proj, sha, ev = _evidence_repo(tmp_path)
    _commit_all(proj, "someone moved HEAD")  # HEAD != ev.head_expected
    refusal = cv.validate_evidence(ev, proj, T, sha)
    assert refusal and "head moved" in refusal.lower()
    assert "replan" in refusal.lower()


# ─── degradation is a plan event bound to the digest ─────────────────────


def _degrading_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo with a tracked-dirty-at-baseline CRLF file the unit edits —
    the delta mechanism cannot isolate it and degrades to full-file."""
    proj = _repo(tmp_path)
    _write(proj, "d.txt", "L1\r\nL2\r\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\n")  # pre-dispatch edit
    _snapshot(proj, sha)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\nUNIT\r\n")  # the unit's edit
    return proj, sha


def test_degradation_requires_explicit_confirmation(tmp_path):
    cv = _cv()
    proj, sha = _degrading_repo(tmp_path)
    # Without confirmation the approval is refused (a warning is not consent).
    built = cv.build_evidence(proj, T, sha)
    assert built.evidence is None
    assert "degradation" in built.refusal.lower()
    assert "d.txt" in built.refusal
    # With the explicit confirmation the full file is bound into the evidence.
    confirmed = cv.build_evidence(proj, T, sha, confirm_degraded=True)
    assert confirmed.evidence is not None, confirmed.refusal
    assert confirmed.evidence.degraded == ("d.txt",)


# ─── binding fields + generated-dependency manifest ──────────────────────


def test_evidence_binds_fields_and_describes_dependencies(tmp_path):
    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _write(proj, "gen.txt", "generated\n")  # pre-existing untracked dep
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")

    built = cv.build_evidence(proj, T, sha)
    ev = built.evidence
    assert ev is not None, built.refusal
    assert ev.candidate_tree
    assert ev.contract_digest and ev.environment_digest and ev.unit_revision
    assert ev.head_expected == sha
    assert len(ev.evidence_digest()) == 64
    # The generated dependency is DESCRIBED, not copied into the candidate.
    assert any(p == "gen.txt" for p, _r in ev.manifest)
    assert built.materialization is not None
    assert not (built.materialization.worktree / "gen.txt").exists()
    assert ev.check_status == cv.CHECK_OK  # verify: ["echo ok"] ran in the candidate


# ─── gate integration: a tampered candidate binding refuses the commit ───


def test_commit_gate_refuses_invalidated_candidate(tmp_git_repo):
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandGate")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj)
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")

    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="pytest -q → ok; verdict: approve", verified_sha=fp)

    signal = proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready"
    binding = json.loads(signal.read_text(encoding="utf-8"))
    assert binding.get("candidate_tree"), "the approve must pin the candidate tree"
    binding["candidate_tree"] = "0" * 40  # tree moved after the approval
    signal.write_text(json.dumps(binding, sort_keys=True) + "\n", encoding="utf-8")

    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status == "refused", outcome.reason
    assert "candidate" in outcome.reason.lower()
    assert _head(proj) == sha, "nothing may be committed on invalidated evidence"


# ─── QA (stage 4/6): independent findings + missing coverage ─────────────


def test_valid_candidate_binding_is_committed(tmp_git_repo):
    """QA coverage gap: only the REFUSED path was tested. A valid bound
    approve must not be falsely refused and must commit."""
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandHappy")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    assert _head(proj) != sha


def test_claim_candidate_is_identical_at_approve_and_gate(tmp_path):
    """QA coverage gap (architect A1): with a CLAIM link the approve-time
    candidate and the gate-time candidate must be the SAME tree — otherwise
    candidate_refusal refuses every claim-bearing run-approve."""
    from awf import commit_plan
    from awf.claim_changes import (
        apply_claim_changes,
        plan_claim_changes,
        write_claim_link,
    )

    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "f.txt", "A\n")
    _commit_all(proj, "init")
    sha = _head(proj)
    (proj / ".agentic" / "context" / "BASELINE-TODO-0002.sha").write_text(sha + "\n")
    _write(proj, "f.txt", "A foreign\n")  # predecessor edit (foreign WIP)
    claim = plan_claim_changes(proj, "TODO-0002", paths=["f.txt"])
    apply_claim_changes(proj, claim, confirmed_digest=claim.digest)
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "U\n")
    write_claim_link(proj, T, "TODO-0002")

    built = cv.build_evidence(proj, T, sha)
    assert built.evidence is not None, built.refusal
    plan = commit_plan.build_commit_plan(proj, T, sha)
    assert built.evidence.candidate_tree == plan.candidate_tree, (
        "approve-time and gate-time candidates must be the SAME tree: "
        f"{built.evidence.candidate_tree} != {plan.candidate_tree}"
    )


def test_confirmed_degradation_commits_the_full_file(tmp_git_repo):
    """Acceptance row 'degrade': with the explicit confirmation the FULL
    worktree file is committed (the acquiesced foreign content included)."""
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandDegOk")
    _write(proj, "d.txt", "L1\r\nL2\r\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\n")  # pre-dispatch foreign edit
    _snapshot(proj, sha)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\nUNIT\r\n")  # the unit's edit
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp, confirm_degraded=True)
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)
    raw = subprocess.run(
        ["git", "cat-file", "blob", "HEAD:d.txt"], cwd=proj, capture_output=True
    ).stdout
    assert raw == b"L1\r\nL2\r\nPRE\r\nUNIT\r\n", raw


def test_refused_conflict_approve_leaves_no_candidate_evidence(tmp_git_repo):
    """FINDING 1 (QA): a conflict-refused approve writes CANDIDATE evidence.

    `approve_commit` builds+writes `context/CANDIDATE-<todo>.json` BEFORE the
    run-diary verdict: a rejection that landed first refuses the approve
    (no APPROVE signal — the A-04 discipline) but the candidate evidence
    FILE is already on disk. Nothing ever removes it, and verify_pack reads
    it (`_candidate_section`), so a rejected unit later reports a VALID
    candidate. A refused operation must not leave evidence behind.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandConflict")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    api.reject_commit(proj, T, "rejected before the approve (QA finding 1)")
    fp = git_utils.tree_fingerprint(proj)
    res = api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    assert res.signal_file == "", "a rejected unit must not publish APPROVE"
    cand = proj / ".agentic" / "context" / f"CANDIDATE-{T}.json"
    assert not cand.exists(), (
        "a refused (conflict) approve must not leave CANDIDATE evidence — "
        "it is read by verify_pack and reports a valid candidate for a unit "
        "that was never approved"
    )


def test_refused_candidate_approve_clears_stale_evidence(tmp_git_repo):
    """FINDING M1 (audit, A-04): a build/refusal reject does NOT clear a
    CANDIDATE-<todo>.json left by an EARLIER successful approve.

    `approve_commit` raises `AwfApiError` on a candidate build/check refusal
    without unlinking the evidence file: a first approve writes it, a second
    (refused) approve leaves it on disk, verify_pack then reports the refused
    unit as a valid candidate. Same A-04 discipline as the conflict branch.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandStaleM1")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _write(proj, "dep.txt", "DEP\n")  # pre-existing untracked, excluded
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    cand = proj / ".agentic" / "context" / f"CANDIDATE-{T}.json"
    assert cand.exists(), "the successful approve must write candidate evidence"

    # The NEXT candidate check needs dep.txt, which the candidate EXCLUDES.
    _write_todo(proj, verify=["cat dep.txt"])
    fp2 = git_utils.tree_fingerprint(proj)
    with pytest.raises(Exception) as exc:
        api.approve_commit(proj, T, evidence="qa", verified_sha=fp2)
    assert "candidate" in str(exc.value).lower()
    assert not cand.exists(), (
        "a refused (build/check) approve must clear the stale CANDIDATE "
        "evidence — verify_pack reads it as a valid candidate for a refused "
        "unit"
    )


def test_refused_precheck_approve_clears_stale_candidate_evidence(tmp_git_repo):
    """PA-1 (audit, A-04, layer 6): the run-mode PRECHECKS fire BEFORE the
    candidate-evidence cleanup, so a stale CANDIDATE-<todo>.json from an
    EARLIER successful approve survives the refusal.

    Two run-mode prechecks raise before the (M1) cleanup helper runs: the
    missing ``evidence=`` one and the missing ``verified_sha=`` one. A
    leftover evidence then makes verify_pack report a refused unit as a
    valid candidate — the A-04 discipline must hold on ENTRY, before any
    precheck or verdict can refuse.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandPrecheck")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    cand = proj / ".agentic" / "context" / f"CANDIDATE-{T}.json"
    run_evidence = proj / ".agentic" / "context" / f"RUN-EVIDENCE-{T}.md"

    def _seed_candidate() -> str:
        fp = git_utils.tree_fingerprint(proj)
        api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
        assert cand.exists(), "the successful approve must write candidate evidence"
        return fp

    # Refusal 1 — the missing-evidence= precheck (run-active, no evidence file).
    _seed_candidate()
    run_evidence.unlink()
    with pytest.raises(Exception):
        api.approve_commit(
            proj, T, evidence="", verified_sha=git_utils.tree_fingerprint(proj)
        )
    assert not cand.exists(), (
        "a refused approve (missing evidence=) must not leave a stale "
        "CANDIDATE evidence from an earlier successful approve — verify_pack "
        "reads it as a valid candidate for a refused unit"
    )

    # Refusal 2 — the missing-verified_sha= precheck.
    _seed_candidate()
    with pytest.raises(Exception):
        api.approve_commit(proj, T, evidence="qa", verified_sha="")
    assert not cand.exists(), (
        "a refused approve (missing verified_sha=) must not leave a stale "
        "CANDIDATE evidence from an earlier successful approve"
    )


def test_conflict_approve_does_not_run_candidate_check(tmp_git_repo, monkeypatch):
    """FINDING M2 (audit): the mandatory candidate check (a full test run in
    a throwaway worktree) runs BEFORE the reject/conflict verdict, so a unit
    already counted as rejected pays for the most expensive path only to
    discard it. The verdict must resolve first; on conflict the check must
    never run."""
    from awf import candidate_verify

    proj = tmp_git_repo
    api.init_project(proj, project_name="CandM2")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    api.reject_commit(proj, T, "reject first (M2)")

    called: list[int] = []
    real = candidate_verify.build_evidence

    def spy(*a, **k):
        called.append(1)
        return real(*a, **k)

    monkeypatch.setattr(candidate_verify, "build_evidence", spy)
    fp = git_utils.tree_fingerprint(proj)
    res = api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    assert res.signal_file == "", "a rejected unit must not publish APPROVE"
    assert called == [], (
        "the mandatory candidate check must not run on an already-rejected "
        "unit (M2) — the conflict verdict is resolved first"
    )


def test_empty_unit_with_a_verify_contract_is_not_refused(tmp_git_repo):
    """FINDING 2 (QA): an empty unit (nothing to commit) is NOT approvable.

    The commit gate handles an empty plan gracefully ('No changes to commit —
    skip commit'); pre-R2.4 the approve of an empty unit succeeded too. R2.4
    now refuses it in `build_evidence` with a factually wrong message ('the
    mandatory candidate check FAILED') — the check never ran, there was no
    tree to check. A no-op/ignore-only/restart-recovery unit stalls the run.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandEmpty")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    # the unit changes nothing (its work is already in HEAD / ignore-only)
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    try:
        res = api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    except Exception as e:  # noqa: BLE001 - the refusal is the finding
        pytest.fail(
            "an empty unit must stay approvable (the gate skips it): the "
            f"candidate check refused an EMPTY candidate instead — {e}"
        )
    assert res.signal_file, "an empty unit's approve must publish the signal"
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status in ("skipped", "committed"), outcome.reason


def test_empty_candidate_binding_is_still_validated(tmp_git_repo):
    """architect-review: the R2.4 discriminator is the PRESENCE of the
    candidate field, not its truthiness.

    An EMPTY candidate (a no-op unit) binds ``candidate_tree == ""`` but
    still carries contract/environment/HEAD/unit_revision. A falsy
    ``if not binding.get("candidate_tree")`` guard skipped every R2.4
    invalidation for such a unit — a contract move after the approval went
    unnoticed. Only a LEGACY binding (no candidate fields at all) may skip.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandEmptyInv")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)

    signal = proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready"
    binding = json.loads(signal.read_text(encoding="utf-8"))
    assert binding.get("candidate_tree") == "", "the unit changes nothing"

    _write_todo(proj, verify=["echo b"])  # the contract moves after approval
    from awf import commit_plan

    plan = commit_plan.build_commit_plan(proj, T, sha)
    refusal = commit_plan.candidate_refusal(plan, proj)
    assert refusal, (
        "an empty candidate's binding must still be validated — a falsy "
        "candidate_tree guard skipped contract/environment/HEAD checks"
    )
    assert "contract changed" in refusal.lower()


def test_tampered_unit_revision_in_binding_is_detected(tmp_git_repo):
    """FINDING 3 (QA, lower): `unit_revision` is bound but never re-derived.

    `CandidateEvidence` binds the unit revision (TODO bytes + baseline) and
    its docstring says the gate re-checks the binding digest; the gate
    (`validate_evidence`) re-derives HEAD / contract / candidate tree /
    environment only. A binding whose `unit_revision` no longer matches the
    evidence (or an evidence_digest other than the one recomputed) passes.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandRev")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    signal = proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready"
    binding = json.loads(signal.read_text(encoding="utf-8"))
    assert binding.get("unit_revision"), "the binding must carry unit_revision"
    binding["unit_revision"] = "0" * 64
    signal.write_text(json.dumps(binding, sort_keys=True) + "\n", encoding="utf-8")
    from awf import commit_plan

    plan = commit_plan.build_commit_plan(proj, T, sha)
    refusal = commit_plan.candidate_refusal(plan, proj)
    assert refusal, (
        "a candidate binding carrying a unit_revision that does not match the "
        "evidence must be refused — the field is bound but never re-derived"
    )


def test_cli_confirm_degraded_flag_reaches_the_api(tmp_git_repo):
    """FINDING 4 (QA): `confirm_degraded` was unreachable from the CLI.

    The API flag existed but ``awf approve`` did not forward it (no CLI
    flag), so a legitimate full-file degradation could not be approved from
    the terminal. End-to-end: without the flag the CLI refuses; with it the
    approval publishes and the FULL worktree file is committed.
    """
    from awf import cli

    proj = tmp_git_repo
    api.init_project(proj, project_name="CandCliDeg")
    _write(proj, "d.txt", "L1\r\nL2\r\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\n")  # pre-dispatch foreign edit
    _snapshot(proj, sha)
    _write(proj, "d.txt", "L1\r\nL2\r\nPRE\r\nUNIT\r\n")  # the unit's edit
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)

    # Without the flag the CLI refuses (a warning is not consent).
    rc = cli.main(
        ["approve", T, "--evidence", "qa", "--verified-sha", fp,
         "--project-dir", str(proj)]
    )
    assert rc == 1
    assert not (proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready").exists()

    # With --confirm-degraded the approval publishes and the full file commits.
    rc = cli.main(
        ["approve", T, "--evidence", "qa", "--verified-sha", fp,
         "--confirm-degraded", "--project-dir", str(proj)]
    )
    assert rc == 0
    assert (proj / ".agentic" / "inbox" / f"APPROVE-{T}.ready").exists()
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status == "committed", (outcome.status, outcome.reason)


# ─── QA (stage 4/6, iter 2): throwaway-worktree leak on refusal paths ─────


def _spy_mkdtemp(monkeypatch) -> list:
    """Track every self-made candidate worktree ``tempfile.mkdtemp`` returns,
    so a leak is caught deterministically (no tmpdir scanning, parallel-safe)."""
    created: list = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        created.append(Path(d))
        return d

    monkeypatch.setattr("awf.candidate_verify.tempfile.mkdtemp", spy)
    return created


def test_degradation_refusal_does_not_leak_worktree(tmp_path, monkeypatch):
    """FINDING 5 (QA): ``build_evidence`` refuses an unconfirmed degradation
    AFTER materialising the candidate worktree and never removes it.

    ``materialize_candidate`` shells the candidate into a ``mkdtemp`` tree
    (the caller ``build_evidence`` owns cleanup for a self-made dir). The
    degradation refusal returns before the discard, so every approve of a
    degrading unit that lacks ``confirm_degraded`` leaves a full checkout in
    the system temp — the very leak the module docstring warns about.
    """
    cv = _cv()
    created = _spy_mkdtemp(monkeypatch)
    proj, sha = _degrading_repo(tmp_path)
    built = cv.build_evidence(proj, T, sha)  # no confirmation -> refusal
    assert built.evidence is None
    assert "degradation" in built.refusal.lower()
    assert created, "the candidate worktree must have been materialised"
    leaked = [d for d in created if d.exists()]
    assert not leaked, f"build_evidence leaked throwaway worktree(s): {leaked}"


def test_validate_evidence_rematerialize_does_not_leak_worktree(tmp_path, monkeypatch):
    """FINDING 6 (QA) / architect-review: ``validate_evidence`` re-derives the
    candidate (the ``candidate_tree is None`` path used by ``verify_pack`` and
    the gate-less callers) but runs NO command — so it must not lay down a
    worktree at all.

    The old fix discarded a self-made checkout after the fact (a full repo
    copy per verify pack). The correct split is ``checkout=False``: the
    validator needs only the plan's tree OID and the degraded set, so no
    ``mkdtemp`` is created and there is nothing to leak.
    """
    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    built = cv.build_evidence(proj, T, sha)
    assert built.evidence is not None, built.refusal
    created = _spy_mkdtemp(monkeypatch)
    refusal = cv.validate_evidence(built.evidence, proj, T, sha)
    assert refusal == "", refusal
    cand = [d for d in created if d.name.startswith("awf-candidate-wt-")]
    assert not cand, (
        "validate_evidence must not lay down a throwaway worktree — it only "
        f"re-derives the tree OID and degraded set (checkout=False): {cand}"
    )


# ─── QA (stage 4/6, iter 3): the cleanup inventory is NOT "every outcome" ──


def test_checkout_failure_does_not_leak_worktree(tmp_path, monkeypatch):
    """FINDING 7 (QA iter 3): a FAILED candidate checkout leaks the self-made
    worktree — cleanup does not happen on EVERY outcome.

    ``materialize_candidate`` creates the ``mkdtemp`` dir, then on a
    ``_checkout_tree`` error returns ``worktree=None`` although the directory
    already exists on disk. The module's own cleanup contract (“a self-made
    dir is removed on every outcome”) is broken: the caller reads
    ``worktree is None`` and therefore never removes the directory.
    """
    cv = _cv()
    created = _spy_mkdtemp(monkeypatch)
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")

    real_run_git = cv._run_git

    def failing_run_git(project_dir, args, env=None):
        if args[:1] == ["read-tree"]:
            return subprocess.CompletedProcess(["git", *args], 1, "", "boom")
        return real_run_git(project_dir, args, env)

    monkeypatch.setattr(cv, "_run_git", failing_run_git)
    m = cv.materialize_candidate(proj, T, sha)
    assert m.worktree is None and m.refusal, m.refusal
    assert created, "the candidate worktree must have been materialised"
    leaked = [d for d in created if d.exists()]
    assert not leaked, (
        "materialize_candidate leaked throwaway worktree(s) on a checkout "
        f"failure: {leaked}"
    )


def test_candidate_build_failure_is_not_treated_as_empty(tmp_path, monkeypatch):
    """FINDING 8 (QA iter 3): a FAILED candidate build is conflated with an
    EMPTY candidate — the mandatory check is silently skipped (fail-open).

    ``_build_candidate_tree`` returns "" both for “no files” and for a git
    staging error; ``materialize_candidate`` treats every empty tree as
    “nothing to verify”, so ``build_evidence`` returns evidence with
    ``check_status == CHECK_UNAVAILABLE`` for a unit that HAS files and whose
    mandatory check never ran. Requirement 3 (mandatory verification of the
    reproducible candidate) is bypassed — a fail-open of exactly the kind
    R2.4 exists to close.
    """
    cv = _cv()
    proj = _repo(tmp_path)
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["false"])  # would FAIL if it ever ran
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")

    import awf.commit_delta as cd

    monkeypatch.setattr(
        cd, "stage_plan_into_index", lambda *a, **k: "git staging failed: boom"
    )
    built = cv.build_evidence(proj, T, sha)
    assert built.evidence is None, (
        "a candidate whose tree FAILED to build must be a refusal, not a "
        "silent 'nothing to verify' approval — the mandatory check never ran "
        f"(check_status={built.evidence and built.evidence.check_status})"
    )
    assert built.refusal


# ─── QA (stage 4/6, iter 4): environment binding + F-QA-8 at the gate ────


def test_env_change_after_approval_invalidates_evidence(tmp_path):
    """Coverage gap (requirement 2): the environment/setup digest is a BOUND
    field, so a changed configured verify command must invalidate the
    evidence exactly like a contract or HEAD move. Only the blob / mode /
    contract / HEAD branches were exercised — the environment branch was
    never hit by a test."""
    cv, proj, sha, ev = _evidence_repo(tmp_path)
    (proj / ".agentic" / "config.yaml").write_text(
        "verification:\n  test_cmd: pytest -q\n", encoding="utf-8"
    )
    refusal = cv.validate_evidence(ev, proj, T, sha)
    assert refusal and "environment changed" in refusal.lower()


def test_gate_refuses_when_candidate_build_fails_after_approval(
    tmp_git_repo, monkeypatch
):
    """F-QA-8 at the GATE: a candidate BUILD failure after the approval must
    refuse the commit — the fail-open of requirement 3 must not reappear
    between approve and publish.

    ``build_evidence`` refuses the approve when the build fails; this locks
    the other end: the approver's candidate tree is a real OID, the
    gate-time build now fails (empty tree), and the binding must catch the
    mismatch instead of committing on a candidate the check never saw.
    """
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandBuildFail")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)

    import awf.commit_delta as cd

    monkeypatch.setattr(
        cd, "stage_plan_into_index", lambda *a, **k: "git staging failed: boom"
    )
    logs = proj / ".agentic" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    outcome = maybe_commit(
        "verify", T, "commit_and_next", proj, logs, auto=False, baseline_sha=sha
    )
    assert outcome.status == "refused", (outcome.status, outcome.reason)
    assert _head(proj) == sha, (
        "nothing may be committed when the candidate could not be built"
    )


# ─── QA (stage 4/6, iter 4): verify_pack surface + explicit degraded set ──


def _approved_run_repo(tmp_git_repo: Path, *, verify: list[str] | None = None):
    """A run-approve repo with R2.4 candidate evidence written."""
    proj = tmp_git_repo
    api.init_project(proj, project_name="CandVp")
    _write(proj, "unit.txt", "base\n")
    _commit_all(proj, "base")
    sha = _head(proj)
    _write_todo(proj, verify=verify if verify is not None else ["echo ok"])
    _snapshot(proj, sha)
    _write(proj, "unit.txt", "unit work\n")
    api.run_start(proj, queue=[T])
    fp = git_utils.tree_fingerprint(proj)
    api.approve_commit(proj, T, evidence="qa", verified_sha=fp)
    return proj, sha


def test_verify_pack_candidate_section_valid_then_invalidated(tmp_git_repo):
    """Coverage gap: the NEW ``verify_pack`` candidate section (R2.4) was never
    exercised. A valid bound candidate reports ``pass``; a contract move after
    the approval invalidates it and the pack fails. This is the surface the
    supervisor reads at verify time (a second consumer of the one validator)."""
    from awf import verify_pack as vp

    proj, _sha = _approved_run_repo(tmp_git_repo)

    res = vp.verify_pack(str(proj), T, write_report=False, tmp_base=proj / ".prove")
    assert res.sections.get("candidate") == "pass", res.sections
    assert res.details.get("candidate") == "candidate binding valid", res.details

    _write_todo(proj, verify=["echo b"])  # the contract moves after approval
    res2 = vp.verify_pack(str(proj), T, write_report=False, tmp_base=proj / ".prove")
    assert res2.sections.get("candidate") == "fail", res2.sections
    assert "invalidated" in (res2.details.get("candidate") or ""), res2.details
    assert res2.exit_code == 1


def test_verify_pack_candidate_validation_does_not_leak_worktree(
    tmp_git_repo, monkeypatch
):
    """F-QA-6 on the PRODUCTION surface + architect-review: ``verify_pack``
    re-derives the candidate inside ``validate_evidence`` (no
    ``candidate_tree`` handed in), but validation runs no command — so no
    worktree may be laid down at all.

    The old fix discarded the checkout; the correct split is ``checkout=False``
    (tree OID only), so a pack run cannot copy (or leak) the repo.
    """
    from awf import verify_pack as vp

    proj, _sha = _approved_run_repo(tmp_git_repo)
    created = _spy_mkdtemp(monkeypatch)
    res = vp.verify_pack(str(proj), T, write_report=False, tmp_base=proj / ".prove")
    assert res.sections.get("candidate") == "pass", res.sections
    cand = [d for d in created if d.name.startswith("awf-candidate-wt-")]
    assert not cand, (
        "verify_pack must not materialise a candidate worktree for a "
        f"binding-only check: {cand}"
    )


def test_validate_evidence_explicit_degraded_set_mismatch_refused(tmp_path):
    """Gate-side degradation binding: the gate hands ``plan.degraded`` to the
    validator (``candidate_refusal``) and the validator must refuse when the
    set no longer matches the evidence — even though the candidate tree OID is
    unchanged. Only the ``degraded is None`` (re-materialise) path was tested."""
    cv = _cv()
    proj, sha = _degrading_repo(tmp_path)
    built = cv.build_evidence(proj, T, sha, confirm_degraded=True)
    assert built.evidence is not None, built.refusal
    assert built.evidence.degraded == ("d.txt",)

    refusal = cv.validate_evidence(
        built.evidence, proj, T, sha,
        candidate_tree=built.evidence.candidate_tree,
        degraded=(),
    )
    assert refusal and "degradation set changed" in refusal.lower(), refusal


# ─── RE-1/RE-2 (iter 7 audit): timeout mechanism + token-bound note ──────


def test_candidate_check_timeout_follows_env_override(tmp_path, monkeypatch):
    """RE-1: the mandatory candidate check must take its timeout from the
    project's single mechanism (``AWF_VERIFY_TIMEOUT`` /
    ``verify._verify_cmd_timeout``), not a hardcoded ``600``. Otherwise a
    raised (or disabled) verify timeout never reaches the most expensive
    path, and a long but healthy suite is ABORTed into a false candidate
    refusal."""
    cv = _cv()
    proj, sha = _excluded_dependency_repo(tmp_path)

    captured: dict = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run_tree(cmd, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _Result()

    monkeypatch.setattr(cv._proc, "run_tree", fake_run_tree)

    def _built_with(env_value: str):
        captured.clear()
        monkeypatch.setenv("AWF_VERIFY_TIMEOUT", env_value)
        built = cv.build_evidence(proj, T, sha, commands=["true"])
        assert built.evidence is not None, built.refusal
        assert "timeout" in captured, "the candidate check must run"
        return captured["timeout"]

    assert _built_with("0") is None, (
        "AWF_VERIFY_TIMEOUT=0 must disable the candidate-check timeout"
    )
    assert _built_with("1234") == 1234, (
        "a raised AWF_VERIFY_TIMEOUT must reach the candidate check"
    )


class _StubPlan:
    """A plan carrying only what ``missing_dependency_note`` reads."""

    def __init__(self, excluded):
        self.excluded = tuple(excluded)


def test_missing_dependency_note_binds_only_whole_path_tokens():
    """RE-2: an excluded path that appears only as a SUBSTRING of unrelated
    output is a different token — a short/common path (``x``, ``lib.py``)
    must not misattribute an ordinary check failure to the excluded
    dependency."""
    cv = _cv()
    plan = _StubPlan([("x", "excluded"), ("lib.py", "excluded")])

    assert cv.missing_dependency_note(plan, "NameError: max( is not defined") == ""
    assert cv.missing_dependency_note(plan, "import mylib.py failed") == ""
    assert cv.missing_dependency_note(plan, "compiled lib.pyc cache") == ""

    bound = cv.missing_dependency_note(plan, "cat: lib.py: No such file or directory")
    assert bound, "a whole-token occurrence must still bind"
    assert "lib.py" in bound and "EXCLUDED" in bound
