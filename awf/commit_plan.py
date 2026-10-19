"""R-03 (audit 2026-09-25, layer 11): the commit as a checkable object.

The gate used to trust loose pieces: a bool return, a signal file, a
run-diary entry, a VERIFIED file — each read separately. Now the commit is
described by ONE explicit object (:class:`CommitPlan`, built at
approve/verify time) and the gate answers with a typed
:class:`CommitOutcome` (committed/skipped/refused/error + reason) that the
execute and verify stages handle identically.

The A-04 verdict check and the A-15 fingerprint check live here, keyed off
the plan's own fields — the gate has no separate duplicate checks
(they used to run once in ``maybe_commit`` and once more inside the
commit path).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from . import commit_delta, git_utils, paths, run_state
from ._log import log as _log

# R2.3: the baseline-fact readers, the file-set computation and the delta
# algorithm now live in the DeltaAssembler module (one home); re-exported
# here so every existing importer (commit_gate, tests) keeps working
# unchanged. The raw passes are re-exported for back-compat — the assembler
# calls its own copies, never back into this module.
PlanSplit = commit_delta.PlanSplit
staged_at_baseline = commit_delta.staged_at_baseline
worktree_dirty_at_baseline = commit_delta.worktree_dirty_at_baseline
read_worktree_snapshot = commit_delta.read_worktree_snapshot
_legacy_plan_split = commit_delta._legacy_plan_split
_all_changed_files = commit_delta._all_changed_files

OUTCOME_COMMITTED = "committed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

#: The reason an empty plan (no diff vs baseline) produces. ONE literal,
#: shared by the gate that emits it and the verify-stage archive gate that
#: must refuse to archive an unexplained empty plan — the strict path keys
#: on this constant, never on a copied prose prefix.
NO_CHANGES_REASON = "no changes to commit"

PROCEED_STATUSES = (OUTCOME_COMMITTED, OUTCOME_SKIPPED)


@dataclass(frozen=True)
class CommitPlan:
    """R-03: everything the gate needs to make ONE unit commit.

    - ``todo_id`` — the unit the commit belongs to.
    - ``generation`` — the active run's generation at build time (cycle
      identity, 0 outside a run). A diary verdict carrying a different
      generation belongs to another cycle and is not enforced.
    - ``verified_sha`` — the stored VERIFIED fingerprint: "" when the
      approve carried no fingerprint (nothing to re-check), None when the
      file exists but could not be read (fail closed).
    - ``files`` — the EXACT commit content: the isolated index commits
      nothing else.
    - ``expected_verdict`` — the run-diary verdict the plan expects for
      this TODO (the gate runs on an approval).
    """

    todo_id: str
    generation: int
    verified_sha: str | None
    files: tuple[str, ...]
    expected_verdict: str = "approved"
    # TODO-0195: the baseline the file set was read against ("" when the
    # no-baseline full-change-set path was used). The gate's quiet-diff
    # check needs it to find the unit's own commit in baseline..HEAD.
    baseline_sha: str = ""
    # TODO-0195: what the plan leaves OUTSIDE the unit commit, as
    # (path, reason) pairs — reasons: "staged-at-baseline" (foreign WIP in
    # the index at dispatch), "staged" (legacy fallback: no baseline
    # status snapshot, the staged-vs-baseline proxy), "pre-existing
    # untracked". () when the no-baseline path was used or nothing was
    # excluded.
    excluded: tuple[tuple[str, str], ...] = ()
    # TODO-0195: paths the worker staged AFTER the baseline (a hard-rule
    # violation) — they ARE in ``files`` (the worker's own work joins the
    # unit commit); the gate warns, it does not refuse. () otherwise.
    worker_staged: tuple[str, ...] = ()
    # REPORTS33 (TODO-0204): the tracked-dirty-at-baseline files in
    # ``files`` — the commit gate commits only the unit's own edit for
    # them (worktree vs the ``BASELINE-{todo}.worktree`` snapshot), so
    # the pre-dispatch WIP stays in the worktree. () when the delta
    # mechanism is unavailable (legacy behavior: the whole diff).
    delta_files: tuple[str, ...] = ()
    # R2.3: a model refusal (unsupported gitlink / unmerged / sparse / split
    # index, or a mixed-ownership rename pair) — the gate returns
    # OUTCOME_REFUSED with this reason BEFORE staging anything; never a
    # silent legacy commit. "" when the plan is usable.
    refusal: str = ""
    # R2.4 / добор 0222: the FULL CandidatePlan is consumed on the prod path
    # now — the candidate tree OID and the degraded (full-file) paths ride on
    # the plan, so :func:`candidate_refusal` pins the candidate the gate will
    # actually commit (not a flattened file list). "" / () when the no-baseline
    # path was used. ``CandidatePlan.baseline_digest`` / ``head_expected`` are
    # NOT copied: the gate re-derives HEAD through the evidence validator and
    # reads no baseline digest, so those two were write-only here.
    candidate_tree: str = ""
    degraded: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitOutcome:
    """R-03: the typed gate result — committed/skipped/refused/error.

    ``reason`` explains refused/error ("" otherwise); ``sha`` carries the
    commit sha on committed ("" otherwise).
    """

    status: str
    reason: str = ""
    sha: str = ""


def _claimed_paths(project_dir: Path, todo_id: str) -> list[str]:
    """REVIEW-F1: the CODE-claim channel a unit adopted (R2.2), resolved from
    its audit links through the ONE transfer-exclusion mechanism (TODO-0221).

    A dispatch with ``claim_from`` writes ``BASELINE-<todo>.claim`` and excludes
    the claimed paths from ``BASELINE-<todo>.untracked``. The DeltaAssembler
    must know the claim, or a claimed tracked predecessor change is treated as
    pre-existing and silently dropped from the unit commit. Best-effort: a
    missing / unreadable link, or an import failure, resolves to ``[]`` (the
    caller then keeps the legacy behavior) — never an exception on the commit
    path.
    """
    if not todo_id:
        return []
    try:
        from .api.hygiene import transfer_exclusions

        _carry_over, _include, claim = transfer_exclusions(project_dir, todo_id)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        # REVIEW-M1: never SILENT — a swallowed failure here drops the claim
        # channel (the class F1 closed: a claimed predecessor change silently
        # leaves the unit commit). Log the same warning shape as the legacy
        # sibling, then degrade to the legacy behavior.
        logs_dir = project_dir / ".agentic" / "logs"
        if logs_dir.is_dir():
            _log(
                logs_dir,
                f"WARNING: claim-channel resolution failed for {todo_id} "
                f"({type(exc).__name__}: {exc}) — claiming is degraded",
            )
        return []
    return sorted(claim)


def _candidate_plan(
    project_dir: Path,
    baseline_sha: str,
    todo_id: str = "",
    claim: tuple[str, ...] | list[str] | None = None,
    *,
    build_tree: bool = True,
) -> tuple[commit_delta.CandidatePlan | None, str]:
    """R2.4: the FULL candidate plan or (None, refusal).

    The prod path consumes the whole :class:`~awf.commit_delta.CandidatePlan`
    — candidate tree, typed operations, claimed transfers, degraded paths —
    not a flattened file list. A model refusal (unsupported state /
    mixed-ownership rename pair) is returned as the refusal text (one home:
    the assembler's).

    ``claim`` — an explicit adoption (R2.2). ``None`` resolves the unit's own
    claim channel through :func:`_claimed_paths`, so the approve-time
    candidate and the gate-time candidate are assembled from the SAME claim
    (one home: :func:`candidate_verify._build_plan` delegates here).

    ``build_tree`` — materialise the candidate tree OID. ``False`` only
    projects the file set / exclusions (:func:`_plan_split`, the legacy
    reader) and skips the isolated-index staging.

    This is the ONE assembler invocation: :func:`_plan_split` projects its
    result instead of calling :class:`DeltaAssembler` a second time.
    """
    if claim is None:
        claim = _claimed_paths(project_dir, todo_id)
    try:
        plan = commit_delta.DeltaAssembler(
            project_dir, todo_id, baseline_sha, claim=claim
        ).assemble(strict=True, build_tree=build_tree)
    except (commit_delta.GitModelError, commit_delta.DeltaConflictError) as exc:
        return None, str(exc)
    return plan, ""


def _plan_split(project_dir: Path, baseline_sha: str, todo_id: str = "") -> PlanSplit:
    """R2.3: the unit's commit file set + exclusions — now ONE assembly.

    The legacy baseline-diff pass (:func:`_legacy_plan_split`) supplies the
    raw set; the :class:`~awf.commit_delta.DeltaAssembler` adds the TYPED
    rename-pair ownership on top, re-admits the explicit claim channel
    (:func:`_claimed_paths`, R2.2) as full files, and records any model
    refusal. A model refusal (unsupported gitlink / unmerged / sparse / split
    index, or a mixed-ownership rename pair) BLOCKS the plan — the gate turns
    it into a refusal, never a silent legacy commit.

    Returns an empty :class:`PlanSplit` on a git error or no baseline (the
    gate degrades to its no-changes path, as before).

    ``PlanSplit`` is the flattened legacy projection of the same
    :func:`_candidate_plan` the prod path consumes — no second assembler
    call, so the file set cannot drift from the candidate's.
    """
    if not baseline_sha:
        return PlanSplit(files=[])
    candidate, refusal = _candidate_plan(
        project_dir, baseline_sha, todo_id, build_tree=False
    )
    if candidate is None:
        # An unsupported/unrepresentable state, or a mixed-ownership rename
        # pair that cannot be isolated without stealing foreign work (roadmap
        # R2.3): refuse the whole unit — never a silent legacy commit, never a
        # silent drop of the pair. The refusal text is the assembler's (one
        # home) — the plan does not restate it.
        return PlanSplit(files=[], refusal=refusal)
    return PlanSplit(
        files=list(candidate.files),
        excluded=list(candidate.excluded),
        worker_staged=list(candidate.worker_staged),
        legacy_fallback=candidate.legacy_fallback,
        delta_files=list(candidate.delta_files),
    )


def _files_changed_since_baseline(
    project_dir: Path,
    baseline_sha: str,
    todo_id: str = "",
) -> list[str]:
    """A1 fix: list files changed since baseline SHA.

    Thin compatibility wrapper over :func:`_plan_split` (TODO-0195): the
    commit set only, without the exclusion facts. Returns relative paths
    (POSIX). Empty list on error or no baseline.
    """
    return _plan_split(project_dir, baseline_sha, todo_id=todo_id).files


def read_baseline_sha(project_dir: Path, todo_id: str) -> str:
    """The ``BASELINE-{todo_id}.sha`` content — the plan's file-set input.

    "" when the baseline file is absent or unreadable (the plan then falls
    back to the full change set, exactly like the gate's no-baseline path).
    """
    if not todo_id:
        return ""
    sha_file = paths.context_dir(project_dir) / f"BASELINE-{todo_id}.sha"
    if not sha_file.is_file():
        return ""
    try:
        return sha_file.read_text(encoding="utf-8").strip().split("\n")[0]
    except OSError:
        return ""


def plan_files(
    project_dir: Path, baseline_sha: str = "", todo_id: str = ""
) -> list[str]:
    """M2.1 legacy reader: the EXACT commit file set, as the plan reads it.

    The baseline diff (A1 isolation) when ``baseline_sha`` is given, the
    full change set (legacy back-compat) otherwise.

    R2.4: the prod plan path no longer goes through this function —
    :func:`build_commit_plan` consumes the full :func:`_candidate_plan`
    (candidate tree + typed operations), and the approve-time binding reuses
    the evidence ``files_digest``. This stays for the legacy/no-baseline
    fallbacks (:func:`_files_changed_since_baseline`, the no-candidate
    approve branch) and for tests. It delegates to the SAME
    :class:`~awf.commit_delta.DeltaAssembler`, so its file set cannot drift
    from the candidate's.
    """
    if baseline_sha:
        return _plan_split(project_dir, baseline_sha, todo_id=todo_id).files
    return _all_changed_files(project_dir)


def files_digest(files: list[str] | tuple[str, ...]) -> str:
    """M2.1: stable digest of the exact commit file set.

    sha256 over the NUL-joined sorted paths — order-insensitive, empty set
    has its own (deterministic) digest.
    """
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _read_verified_sha(project_dir: Path, todo_id: str) -> str | None:
    """The stored VERIFIED-{todo_id}.sha fingerprint.

    "" when the approve carried no fingerprint (file absent — nothing to
    re-check), None when the file exists but could not be read (fail
    closed), the fingerprint otherwise.
    """
    verified: str | None = ""
    if todo_id:
        verified_file = paths.context_dir(project_dir) / f"VERIFIED-{todo_id}.sha"
        if verified_file.is_file():
            try:
                verified = verified_file.read_text(encoding="utf-8").strip().lower()
            except OSError:
                verified = None
    return verified


def refresh_verified_sha(plan: CommitPlan, project_dir: Path) -> CommitPlan:
    """REVIEW-0087 P2: re-read the stored VERIFIED fingerprint into a plan.

    ``build_commit_plan`` runs BEFORE the gate's APPROVE/ACK wait; an
    approve that arrives in the wait window writes VERIFIED-{todo}.sha
    after the build, so ``plan.verified_sha`` would stay "" and the A-15
    check silently skips. This updates the plan's field only — the check
    itself stays in :func:`fingerprint_refusal` (one mechanism, no
    duplicate check).
    """
    return replace(plan, verified_sha=_read_verified_sha(project_dir, plan.todo_id))


def build_commit_plan(
    project_dir: Path,
    todo_id: str,
    baseline_sha: str = "",
) -> CommitPlan:
    """R-03: build the plan at approve/verify time — everything the gate
    needs, read once, passed to the gate as a unit.

    - ``generation``: the active run's identity (0 outside a run).
    - ``verified_sha``: the stored VERIFIED-{todo_id}.sha content ("" when
      the file is absent — the approve carried no fingerprint; None when
      the file exists but could not be read — fail closed).
    - ``files``: the baseline diff (A1 isolation) when ``baseline_sha`` is
      given, the full change set (legacy ``git add -A`` back-compat)
      otherwise.
    - ``excluded`` / ``worker_staged`` (TODO-0195): what the plan leaves
      outside the commit (path + reason) and what the worker staged after
      the baseline (included, the gate warns) — the same pass that built
      ``files``, so the facts always describe the committed set.
    """
    generation = 0
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        state = None
    if state and state.get("active"):
        generation = run_state.generation_of(state)

    verified = _read_verified_sha(project_dir, todo_id)

    if baseline_sha:
        candidate, refusal = _candidate_plan(project_dir, baseline_sha, todo_id)
        if candidate is None:
            return CommitPlan(
                todo_id=todo_id,
                generation=generation,
                verified_sha=verified,
                files=(),
                baseline_sha=baseline_sha,
                refusal=refusal,
            )
        return CommitPlan(
            todo_id=todo_id,
            generation=generation,
            verified_sha=verified,
            files=tuple(candidate.files),
            baseline_sha=baseline_sha,
            excluded=tuple(candidate.excluded),
            worker_staged=tuple(candidate.worker_staged),
            delta_files=tuple(candidate.delta_files),
            candidate_tree=candidate.candidate_tree,
            degraded=tuple(candidate.degraded),
        )
    return CommitPlan(
        todo_id=todo_id,
        generation=generation,
        verified_sha=verified,
        files=tuple(_all_changed_files(project_dir)),
    )


def verdict_refusal(plan: CommitPlan, project_dir: Path) -> str:
    """A-04 (audit 2026-09-25, layer 4): the active run's diary check.

    A leftover or raced APPROVE/ACK signal must not unlock a commit the
    run already rejected: when the ACTIVE run's diary records a verdict
    for this TODO that differs from ``plan.expected_verdict``, the gate
    refuses before staging anything. Only an active run has verdict
    authority — outside a run there is no diary, and a fresh run resets
    it (``outcomes={}``), so a stale verdict cannot outlive its cycle. A
    'generation' marker on the entry (when present) that differs from
    ``plan.generation`` belongs to another cycle and is not enforced; an
    unreadable marker fails closed (enforce).

    Returns a refusal reason, or "" when the commit may proceed.
    """
    if not plan.todo_id:
        return ""
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        return ""  # unreadable run state — the verdict is not provable
    if not state or not state.get("active"):
        return ""
    outcome = (state.get("outcomes") or {}).get(plan.todo_id)
    if not isinstance(outcome, dict) or outcome.get("verdict") == plan.expected_verdict:
        return ""
    if "generation" in outcome:
        try:
            if int(outcome["generation"]) != plan.generation:
                return ""  # verdict of another cycle — not this run's
        except (TypeError, ValueError):
            pass  # unreadable marker — fail closed, enforce the verdict
    reason = str(outcome.get("reason") or "").strip()
    return (
        f"the active run's diary records verdict {outcome.get('verdict')!r} "
        f"for {plan.todo_id} but the plan expects {plan.expected_verdict!r}"
        + (f" ({reason[:200]})" if reason else "")
    )


def fingerprint_refusal(plan: CommitPlan, project_dir: Path) -> str:
    """A-15 (audit 2026-09-25, layer 4): the tree vs the plan's fingerprint.

    ``approve(verified_sha=...)`` stores the verified tree fingerprint;
    between the approve and the commit the tree may still move (an edit,
    a new file, a commit by someone else) — the gate would then commit
    something that was not verified. The plan carries the stored value:
    "" = the approve carried no fingerprint (legacy behavior: nothing is
    checked); None = the file exists but is unreadable (fail closed).
    Otherwise the fingerprint is recomputed (``git_utils.tree_fingerprint``,
    the same mechanism approve uses) and a mismatch refuses the commit.

    Must run while the tree is still PRE-STAGING: ``tree_fingerprint`` is
    staging-sensitive (``git add`` flips the status flags and drops the
    files from the untracked list), so a post-add comparison would refuse
    the gate's own staging and break the verified success path.

    Returns a refusal reason, or "" when the commit may proceed.
    """
    if not plan.todo_id:
        return ""
    expected = plan.verified_sha
    if expected is None:
        return (
            f"the verified fingerprint file VERIFIED-{plan.todo_id}.sha exists "
            "but could not be read — refuse to commit unverified (fail closed)"
        )
    if expected == "":
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        return (
            f"the verified fingerprint file VERIFIED-{plan.todo_id}.sha does "
            "not hold a 64-hex tree fingerprint — refuse to commit "
            "unverified (fail closed); re-verify and re-approve with a "
            "fresh `awf tree-sha`"
        )
    try:
        current = git_utils.tree_fingerprint(project_dir)
    except RuntimeError:
        return (
            f"the verified fingerprint {expected[:12]}… exists but the tree "
            "fingerprint could not be recomputed — refuse to commit "
            "unverified (fail closed)"
        )
    if current != expected:
        return (
            f"the tree changed after verification (verified {expected[:12]}…, "
            f"now {current[:12]}…) — the commit gate would commit something "
            "that was not verified. Re-verify on the current tree "
            "(`awf tree-sha`) and re-approve with the fresh fingerprint."
        )
    return ""


def read_approval_binding(project_dir: Path, todo_id: str) -> dict | None:
    """M2.1: the approval binding stored in ``APPROVE-{todo_id}.ready``.

    In an active run ``awf_approve`` writes the signal as a small JSON:
    ``{"generation": int, "verified_sha": 64-hex, "files_digest": 64-hex}``
    — the run generation the decision was made in, the verified-tree
    fingerprint, and the digest of the file set the gate will apply.
    Outside a run the signal stays an empty marker. An empty or
    unparseable file (legacy approve, a hand-made signal) yields None —
    the gate fail-closes it inside a run.
    """
    signal = paths.inbox(project_dir) / f"APPROVE-{todo_id}.ready"
    try:
        raw = signal.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def binding_refusal(plan: CommitPlan, project_dir: Path, authorized_by: str) -> str:
    """M2.1 (ORCH M2.1): the approval's binding vs the run generation and
    the plan.

    A-04 and A-15 pin the verdict and the tree, but neither pins WHICH run
    cycle the approval belongs to: a force-restarted run resets the diary
    (A-04 then has nothing to check) and an unchanged tree passes A-15, so
    a leftover approval of a dead generation unlocked the new cycle's
    commit. The approve now stamps the binding (generation, verified
    fingerprint, file-set digest) into the APPROVE signal; this check
    enforces it at the gate:

    - only a bound APPROVE authorizes a commit in an ACTIVE run — an ACK
      (or an unbound APPROVE: legacy, hand-made) carries no evidence and
      no fingerprint and is refused (the AUD11-03 evidence gate for the
      interactive wait, now also for the gate itself);
    - the binding's generation must be the CURRENT run's generation — an
      approval published for a revised/restarted run is stale and is
      refused;
    - the binding's fingerprint must be the one the plan will enforce
      (a divergent VERIFIED file fails closed);
    - the plan's file set must match the set the approve covered.

    Outside a run the binding is not enforced (legacy behavior). Returns a
    refusal reason, or "" when the commit may proceed.
    """
    if not plan.todo_id:
        return ""
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        return ""  # unreadable run state — the same posture as A-04
    if not state or not state.get("active"):
        return ""
    current_gen = run_state.generation_of(state)

    if authorized_by != "APPROVE":
        what = (
            "the ACK signal"
            if authorized_by == "ACK"
            else "no approval signal is present"
        )
        return (
            f"an active run (generation {current_gen}) authorizes the unit "
            f"commit only via a bound awf_approve — {what} carries no "
            "evidence and no verified fingerprint. Approve with "
            "awf_approve(evidence=..., verified_sha=...)."
        )

    binding = read_approval_binding(project_dir, plan.todo_id)
    if binding is None:
        return (
            f"the APPROVE-{plan.todo_id}.ready signal carries no binding "
            "(published by an older awf version or by hand) — an active "
            "run commits only on a bound approval. Re-approve with "
            "awf_approve(evidence=..., verified_sha=...)."
        )

    gen = binding.get("generation")
    if isinstance(gen, bool) or not isinstance(gen, int):
        return (
            "the APPROVE signal's binding has no readable generation — "
            "re-approve with awf_approve."
        )
    if gen != current_gen:
        return (
            f"the approve was published for run generation {gen}, but the "
            f"active run is generation {current_gen} — the run was revised "
            "or restarted after the approval. The stale approval must not "
            "unlock this commit. Re-verify on the current run and approve "
            "again (awf_approve)."
        )

    expected_fp = binding.get("verified_sha")
    if not isinstance(expected_fp, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_fp):
        return (
            "the APPROVE signal's binding has no valid verified_sha — "
            "re-approve with awf_approve."
        )
    if plan.verified_sha != expected_fp:
        return (
            "the fingerprint the approve pinned does not match the one the "
            "commit gate will enforce — the VERIFIED file and the approval "
            "diverge. Re-verify on the current tree (`awf tree-sha`) and "
            "re-approve with the fresh fingerprint."
        )

    expected_digest = binding.get("files_digest")
    if (
        not isinstance(expected_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
    ):
        return (
            "the APPROVE signal's binding has no valid files_digest — "
            "re-approve with awf_approve."
        )
    # One home (R2.4): the plan already carries the exact file set it will
    # commit (built with the full CandidatePlan), so the binding is checked
    # against ``plan.files`` — not re-assembled through the legacy split.
    if files_digest(plan.files) != expected_digest:
        return (
            "the commit's file set differs from the set the approve covered "
            "(files digest mismatch) — the gate would commit files the "
            "supervisor did not verify. Re-approve on the current tree."
        )
    return ""


def candidate_refusal(plan: CommitPlan, project_dir: Path) -> str:
    """R2.4: the approval's candidate binding vs the tree the gate will commit.

    The approve pins the CANDIDATE (candidate tree OID, contract digest,
    environment digest, run generation, unit revision, degraded paths) into
    the APPROVE signal. This check re-derives the same facts from the
    current tree and refuses when any moved:

    - HEAD moved → REPLAN (never a commit on top of a new HEAD);
    - the TODO contract changed → evidence invalidated;
    - the candidate content changed (blob/mode/path/degradation) →
      evidence invalidated;
    - the verification environment changed → evidence invalidated.

    A legacy / hand-made binding (no R2.4 candidate fields at all) carries
    no candidate evidence and is left to the M2.1 binding checks (the pre-R2.4
    behavior) — this check cannot invalidate what was never bound. Returns
    a refusal reason, or "" when the commit may proceed.
    """
    if not plan.todo_id:
        return ""
    binding = read_approval_binding(project_dir, plan.todo_id)
    if binding is None:
        return ""
    # The discriminator is the PRESENCE of the R2.4 candidate field, not its
    # truthiness: an EMPTY candidate (nothing to commit) has
    # ``candidate_tree == ""`` yet still binds contract/environment/HEAD/
    # unit_revision — a falsy check here would silently skip every R2.4
    # invalidation for an empty unit.
    if "candidate_tree" not in binding:
        return ""  # legacy binding — no candidate evidence to enforce
    from . import candidate_verify

    stored = candidate_verify.CandidateEvidence.from_dict(
        {**binding, "todo_id": plan.todo_id}
    )
    if stored is None:
        return (
            "the APPROVE signal's candidate evidence is unreadable — "
            "re-verify the candidate and re-approve with fresh evidence."
        )
    # F-QA-3: the binding carries ``evidence_digest`` — the digest of its own
    # binding fields. Re-derive it and refuse when a field was altered without
    # the digest moving (a tampered unit_revision / candidate_tree / etc.).
    expected_digest = binding.get("evidence_digest")
    if isinstance(expected_digest, str) and expected_digest:
        if stored.evidence_digest() != expected_digest:
            return (
                "the APPROVE signal's candidate evidence digest does not match "
                "its binding fields — the binding was altered after the "
                "approval. Re-verify the candidate and re-approve with fresh "
                "evidence."
            )
    # The gate already holds the candidate tree it will commit (the plan was
    # built with ``build_tree=True``), so hand those two facts to the
    # validator instead of re-materialising the candidate a second time. A
    # no-baseline plan carries no candidate tree → ``None`` lets the validator
    # materialise (and refuse) exactly as before.
    return candidate_verify.validate_evidence(
        stored,
        project_dir,
        plan.todo_id,
        plan.baseline_sha,
        candidate_tree=plan.candidate_tree if plan.baseline_sha else None,
        degraded=plan.degraded,
    )


def preexisting_approval_status(
    project_dir: Path,
    todo_id: str,
    baseline_sha: str = "",
) -> tuple[str, str]:
    """REPORTS29 (TODO-0169): a PRE-EXISTING APPROVE validated against the
    current attempt.

    A verify wait that starts AFTER ``APPROVE-{todo_id}.ready`` was written
    (the pipeline died between the approval and its consumption — the
    restart-recovery case) drops the file by mtime (AUD04-04). This helper
    answers whether that drop is right, using the same fields and the same
    validator the commit gate enforces (M2.1, :func:`binding_refusal`):

    - ``("valid", "")`` — the signal carries a binding and it matches the
      current attempt: the ACTIVE run's generation, the stored verified
      fingerprint, and the current file-set digest. A fresh wait may
      consume the signal — the tree it approved is the tree that will be
      committed, and re-approval would republish the same binding.
    - ``("stale", reason)`` — present, but not valid for the current
      attempt (no binding, no active run, or a binding mismatch). ``reason``
      is the human-readable re-approval instruction for the log and for the
      supervisor's hint.
    - ``("absent", "")`` — no APPROVE signal file.

    Outside a run the signal is an empty marker (no binding to validate —
    legacy approve), so a pre-existing approval is always ``stale`` there:
    the AUD04-04 mtime gate keeps its old behavior.
    """
    signal = paths.inbox(project_dir) / f"APPROVE-{todo_id}.ready"
    if not signal.is_file():
        return "absent", ""
    binding = read_approval_binding(project_dir, todo_id)
    if binding is None:
        return "stale", (
            f"the APPROVE-{todo_id}.ready signal carries no binding "
            "(legacy or hand-made) — a pre-existing approval cannot be "
            "validated against the current attempt"
        )
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        state = None
    if not (state and state.get("active")):
        return "stale", (
            f"the APPROVE-{todo_id}.ready binding is only honored inside an "
            "active run — no run is active now"
        )
    plan = build_commit_plan(project_dir, todo_id, baseline_sha)
    reason = binding_refusal(plan, project_dir, "APPROVE")
    if reason:
        return "stale", reason
    return "valid", ""
