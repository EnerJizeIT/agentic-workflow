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
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from . import git_utils, paths, run_state
from ._log import log as _log

OUTCOME_COMMITTED = "committed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_REFUSED = "refused"
OUTCOME_ERROR = "error"

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


@dataclass(frozen=True)
class CommitOutcome:
    """R-03: the typed gate result — committed/skipped/refused/error.

    ``reason`` explains refused/error ("" otherwise); ``sha`` carries the
    commit sha on committed ("" otherwise).
    """

    status: str
    reason: str = ""
    sha: str = ""


def _files_changed_since_baseline(
    project_dir: Path,
    baseline_sha: str,
    todo_id: str = "",
) -> list[str]:
    """A1 fix: list files changed since baseline SHA.

    Returns relative paths (POSIX). Empty list on error or no baseline.

    QA-1: ``todo_id`` used to locate ``BASELINE-{todo_id}.untracked`` snapshot
    so pre-existing untracked files are excluded from worker commit.

    Now combines:
    1. ``git diff --name-only <sha>`` — modified tracked files since baseline.
    2. ``git ls-files --others --exclude-standard`` — new untracked files
       (respects .gitignore — awf runtime dirs stay excluded).
    """
    if not baseline_sha:
        return []

    # 1. Modified tracked files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", baseline_sha],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11: hung git must not hang the commit gate
        )
        if result.returncode != 0:
            logs_dir = project_dir / ".agentic" / "logs"
            if logs_dir.is_dir():
                _log(
                    logs_dir,
                    f"WARNING: git diff vs baseline {baseline_sha!r} failed "
                    f"(rc={result.returncode}): {result.stderr.strip()}",
                )
            return []
        modified = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        return []

    # R-03: subtract what the user's index staged against the baseline.
    # The unit never stages (worker hard rule); anything staged vs baseline
    # is the user's WIP. With the isolated index it can no longer leak into
    # the commit, so the plan EXCLUDES it instead of refusing (A-01).
    try:
        foreign = subprocess.run(
            ["git", "diff", "--cached", "--name-only", baseline_sha],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11
        )
    except (subprocess.SubprocessError, OSError):
        return []  # can't tell what is foreign — no plan, no commit
    if foreign.returncode != 0:
        logs_dir = project_dir / ".agentic" / "logs"
        if logs_dir.is_dir():
            _log(
                logs_dir,
                f"WARNING: git diff --cached vs baseline {baseline_sha!r} failed "
                f"(rc={foreign.returncode}): {foreign.stderr.strip()}",
            )
        return []
    foreign_paths = {
        line.strip() for line in foreign.stdout.splitlines() if line.strip()
    }
    modified = [p for p in modified if p not in foreign_paths]

    # 2. Untracked files (new files worker created since baseline)
    # QA-1: exclude pre-existing untracked files (existed before baseline).
    # Read BASELINE-{todo_id}.untracked snapshot to filter them out.
    try:
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11
        )
        if untracked_result.returncode == 0:
            untracked = [
                line.strip() for line in untracked_result.stdout.splitlines() if line.strip()
            ]
        else:
            untracked = []
    except (subprocess.SubprocessError, OSError):
        untracked = []

    # QA-1: filter out files that were already untracked at baseline time.
    # BASELINE-{todo_id}.untracked was written by create_baseline().
    baseline_untracked_path: Path | None = None
    if todo_id:
        candidate = project_dir / ".agentic" / "context" / f"BASELINE-{todo_id}.untracked"
        if candidate.exists():
            baseline_untracked_path = candidate
    if not baseline_untracked_path:
        # AUD-2026-08-09.3: no baseline for this todo_id → don't grab another
        # TODO's baseline (would include wrong files). Log warning, include all
        # untracked (safer than filtering with wrong data).
        if todo_id:
            print(
                f"warning: no BASELINE-{todo_id}.untracked found, "
                "including all untracked files in commit",
                file=sys.stderr,
            )

    if baseline_untracked_path and baseline_untracked_path.exists():
        try:
            pre_existing = {
                line.strip()
                for line in baseline_untracked_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            untracked = [f for f in untracked if f not in pre_existing]
        except OSError:
            pass  # best effort — if can't read, include all untracked

    # Combine + dedupe (a file could be in both lists if it was deleted
    # then re-created). Order: modified first, then new untracked.
    seen: set[str] = set()
    combined: list[str] = []
    for path in [*modified, *untracked]:
        if path and path not in seen:
            seen.add(path)
            combined.append(path)
    return combined


def _all_changed_files(project_dir: Path) -> list[str]:
    """No-baseline back-compat file set: everything ``git add -A`` used to
    stage — tracked changes (staged, unstaged, deleted) plus untracked.

    NUL-separated porcelain so unusual path names survive verbatim. A
    staged rename/copy is TWO tokens ("R<state> target" + the bare source
    name, state = the target's worktree status `` ``/``M``/``D``); both
    are the user's index WIP and are consumed whole for every state,
    neither contributes a path — the source token is not a path, and a
    source name that looks like a status prefix (``git mv '?a b' ...``)
    must not be parsed as one (REVIEW-0087 P3). Consuming the pair for
    every state (not only the clean "R " one) is what keeps the RM/CM
    pair's bare source token out of the path branch (REVIEW-0087 P4;
    REVIEW-0104 F1: a worktree existence check on path entries was wider
    than this angle — it dropped real " D" deletions, and a source tail
    that happens to exist in the worktree would still leak). Entries the
    user's index staged (R-03: the unit never stages — a staged-vs-HEAD
    entry is the user's WIP and stays out of the plan, same rule as the
    baseline path). Empty list on error.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11
        )
        if result.returncode != 0:
            return []
        tokens = result.stdout.split("\0")
        files: list[str] = []
        seen: set[str] = set()
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if (
                len(tok) >= 4
                and tok[0] in ("R", "C")
                and tok[1] in (" ", "M", "D")
            ):
                # staged rename/copy, any worktree state of the target
                # ("R  target" / "RM target" / "RD target" + the bare
                # source token) — the user's index WIP, consume both
                # tokens, keep neither
                i += 2
                continue
            if len(tok) >= 4 and tok[2] == " " and tok[0] in (" ", "?"):
                path = tok[3:]
                if path and path not in seen:
                    seen.add(path)
                    files.append(path)
            i += 1
        return files
    except (subprocess.SubprocessError, OSError):
        return []


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
    """M2.1: the EXACT commit file set, read the way the plan reads it.

    The baseline diff (A1 isolation) when ``baseline_sha`` is given, the
    full change set (legacy back-compat) otherwise. ``build_commit_plan``
    and the approve-time binding both go through this one function, so
    their digests are computed over the same file set by construction.
    """
    if baseline_sha:
        return _files_changed_since_baseline(project_dir, baseline_sha, todo_id=todo_id)
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
    """
    generation = 0
    try:
        state = run_state.read_run(project_dir)
    except Exception:
        state = None
    if state and state.get("active"):
        generation = run_state.generation_of(state)

    verified = _read_verified_sha(project_dir, todo_id)

    files = plan_files(project_dir, baseline_sha, todo_id)
    return CommitPlan(
        todo_id=todo_id,
        generation=generation,
        verified_sha=verified,
        files=tuple(files),
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
    files = plan_files(
        project_dir, read_baseline_sha(project_dir, plan.todo_id), plan.todo_id
    )
    if files_digest(files) != expected_digest:
        return (
            "the commit's file set differs from the set the approve covered "
            "(files digest mismatch) — the gate would commit files the "
            "supervisor did not verify. Re-approve on the current tree."
        )
    return ""


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
