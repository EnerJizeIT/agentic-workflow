"""BD-8/17: commit gate — auto-commit with optional APPROVE/ACK signal wait.

Extracted from orchestrator.py (A6 refactor).

A1 fix: commit only files changed since baseline (not `git add -A`).
Supervisor's mid-flight edits stay out of worker commits.

R-03 (audit 2026-09-25, layer 11): the commit is an explicit object —
``commit_plan.CommitPlan`` (todo id, run generation, verified fingerprint,
exact file list, expected verdict) built at approve/verify time — and the
gate returns a typed ``commit_plan.CommitOutcome`` (committed/skipped/
refused/error + reason) that the execute and verify stages handle the same
way. The A-04 verdict and A-15 fingerprint checks are plan checks
(``commit_plan``); the commit runs through a throwaway ``GIT_INDEX_FILE``
so the user's index is never opened — foreign staged entries can neither
leak into the unit commit nor be unstaged by a failure rollback (A-01).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import commit_plan, git_utils, paths
from ._log import log as _log

APPROVE_POLL_INTERVAL: int = 2


def _safe_int_env(name: str, default: int) -> int:
    """Parse int from env var with safe fallback on invalid value.

    Without this, ``AWF_APPROVE_TIMEOUT_SECONDS=1800s`` (typo) crashed the
    whole ``awf.commit_gate`` module on import. Now falls back to default
    and logs nothing — typo is a config mistake, not a crash-worthy event.
    """
    raw = os.environ.get(name, str(default))
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return default


def _get_approve_timeout() -> int:
    """P3: lazy evaluation — reads env at call time, not import time.

    Allows tests to set AWF_APPROVE_TIMEOUT_SECONDS via monkeypatch
    without restarting Python.
    """
    return _safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800)


def _has_head(project_dir: Path) -> bool:
    """Does the repo have any commit yet (the first-commit case)?"""
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            timeout=30,  # AUD04-11
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _git_isolated(
    project_dir: Path, args: list[str], env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Run one git command against the plan's throwaway index (R-03).

    ``GIT_INDEX_FILE`` keeps every index operation — read-tree, add,
    diff --cached, the commit itself, and the hooks' own git calls —
    inside the temporary file. The user's ``.git/index`` is never opened.
    """
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,  # AUD04-11: hung git must not hang the commit gate
        env=env,
    )


def _commit_via_isolated_index(
    project_dir: Path,
    plan: commit_plan.CommitPlan,
    message: str,
) -> commit_plan.CommitOutcome:
    """R-03: commit EXACTLY ``plan.files`` through a throwaway index.

    The isolated index is built from HEAD (when commits exist) plus the
    plan's files and deleted in ``finally``. On success the user's index
    is unchanged (foreign staged entries stay staged); on refusal or
    error it is byte-identical (nothing was ever staged into it).
    """
    fd, index_path = tempfile.mkstemp(
        prefix=f"awf-commit-index-{plan.todo_id}-", suffix=".idx"
    )
    os.close(fd)
    env = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        try:
            # A 0-byte file is NOT a valid index for git (git 2.43: "index
            # file smaller than expected") — initialize it properly first.
            init = _git_isolated(project_dir, ["read-tree", "--empty"], env)
            if init.returncode != 0:
                return commit_plan.CommitOutcome(
                    commit_plan.OUTCOME_ERROR,
                    f"git read-tree --empty failed (rc={init.returncode}): "
                    f"{init.stderr.strip()}",
                )
            if _has_head(project_dir):
                read = _git_isolated(project_dir, ["read-tree", "HEAD"], env)
                if read.returncode != 0:
                    return commit_plan.CommitOutcome(
                        commit_plan.OUTCOME_ERROR,
                        f"git read-tree HEAD failed (rc={read.returncode}): "
                        f"{read.stderr.strip()}",
                    )
            add = _git_isolated(project_dir, ["add", "--", *plan.files], env)
            if add.returncode != 0:
                return commit_plan.CommitOutcome(
                    commit_plan.OUTCOME_ERROR,
                    f"git add failed (rc={add.returncode}): "
                    f"{add.stderr.strip() or add.stdout.strip()}",
                )
            if _has_head(project_dir):
                diff = _git_isolated(project_dir, ["diff", "--cached", "--quiet"], env)
                if diff.returncode == 0:
                    return commit_plan.CommitOutcome(
                        commit_plan.OUTCOME_SKIPPED,
                        "the plan's files match HEAD — nothing to commit",
                    )
            result = _git_isolated(project_dir, ["commit", "-m", message], env)
        except (subprocess.SubprocessError, OSError) as e:
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR, f"git commit failed: {e}"
            )
        if result.returncode != 0:
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR,
                f"git commit failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
        sha = _git_isolated(project_dir, ["rev-parse", "--short", "HEAD"], env)
        if sha.returncode != 0:
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR,
                "the commit was created but its sha could not be read",
            )
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_COMMITTED, "", sha.stdout.strip()
        )
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
    auto: bool = False,
    baseline_sha: str = "",
) -> commit_plan.CommitOutcome:
    """Auto-commit if policy is commit_and_next or commit_and_report.

    R-03: returns the typed ``CommitOutcome`` (committed/skipped/
    refused/error + reason). The execute stage and the verify stage
    handle it the same way — a refused/failed commit stops the cycle
    (the boolean result used to be ignored on the execute path, A-14).
    In auto mode, blocks waiting for APPROVE-TODO-NNNN.ready (or ACK)
    before committing. This preserves supervisor approval gate.

    The plan is built at approve/verify time (``commit_plan.
    build_commit_plan``): the verdict check (A-04) runs BEFORE the signal
    wait — a rejected unit must not commit even when a leftover signal is
    present; the fingerprint re-check (A-15) runs pre-staging, from the
    plan's stored value. In auto mode the stored fingerprint is re-read
    into the plan AFTER the signal wait — an approve that arrives in the
    wait window writes VERIFIED-{todo}.sha after the build, and its
    fingerprint must be enforced, not silently skipped (REVIEW-0087 P2).
    Both are plan checks — there is no separate duplicate check in the
    commit path.
    """
    if policy not in ("commit_and_next", "commit_and_report"):
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, f"policy {policy!r} does not commit"
        )

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return commit_plan.CommitOutcome(commit_plan.OUTCOME_SKIPPED, "not a git repo")

    plan = commit_plan.build_commit_plan(project_dir, todo_id, baseline_sha)

    refusal = commit_plan.verdict_refusal(plan, project_dir)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"A-04: commit refused for {todo_id} — {refusal}")
        return commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal)

    if auto:
        inbox = paths.inbox(project_dir)
        # BD-17: accept either APPROVE-{todo}.ready (from `awf approve` /
        # human) or ACK-{todo}.ready (from supervisor verify subprocess in
        # auto mode). Both authorize the commit.
        approve_signal = inbox / f"APPROVE-{todo_id}.ready"
        ack_signal = inbox / f"ACK-{todo_id}.ready"
        print("Auto-mode: waiting for supervisor approval to commit.", file=sys.stderr)
        print(f"  Approve signal: awf approve {todo_id}", file=sys.stderr)
        print("  Or ACK from supervisor verify subprocess.", file=sys.stderr)
        _log(logs_dir, f"Auto-mode: waiting for APPROVE or ACK signal for {todo_id}")

        # H1 fix: time.monotonic() not time.time() — NTP-immune.
        deadline = time.monotonic() + _get_approve_timeout()
        while not approve_signal.exists() and not ack_signal.exists():
            if time.monotonic() > deadline:
                print(
                    f"ERROR: APPROVE/ACK signal not received within {_get_approve_timeout()}s. "
                    f"Pipeline aborting.",
                    file=sys.stderr,
                )
                _log(logs_dir, f"APPROVE/ACK timeout for {todo_id}")
                raise TimeoutError(f"APPROVE/ACK signal not received for {todo_id}")
            time.sleep(APPROVE_POLL_INTERVAL)
        which = "APPROVE" if approve_signal.exists() else "ACK"
        _log(logs_dir, f"{which} signal received for {todo_id}")

        # REVIEW-0087 P2: the plan was built BEFORE the wait; an approve
        # that arrived in the window wrote VERIFIED-{todo}.sha after the
        # build, so plan.verified_sha is "" and the A-15 check would
        # silently skip. Re-read the stored fingerprint into the plan
        # (field update only — the check stays in fingerprint_refusal).
        plan = commit_plan.refresh_verified_sha(plan, project_dir)

    refusal = commit_plan.fingerprint_refusal(plan, project_dir)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"A-15: commit refused for {todo_id} — {refusal}")
        return commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal)

    if not plan.files:
        print(f"No changes to commit — skip commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"A1: no diff vs baseline at {stage_name}")
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, "no changes to commit"
        )

    outcome = _commit_via_isolated_index(project_dir, plan, f"awf({stage_name}): {todo_id}")
    if outcome.status == commit_plan.OUTCOME_COMMITTED:
        print(f"Auto-committed: {todo_id} at '{stage_name}' ({outcome.sha}).", file=sys.stderr)
        print("Remember to push: git push origin HEAD", file=sys.stderr)
        _log(logs_dir, f"Auto-committed {todo_id} at {stage_name} ({outcome.sha})")
    elif outcome.status == commit_plan.OUTCOME_SKIPPED:
        print(f"No commit at '{stage_name}': {outcome.reason}", file=sys.stderr)
        _log(logs_dir, f"Commit skipped at {stage_name}: {outcome.reason}")
    else:
        print(
            f"Commit gate {outcome.status} at '{stage_name}' for {todo_id}: "
            f"{outcome.reason}",
            file=sys.stderr,
        )
        _log(
            logs_dir,
            f"Commit {outcome.status} at {stage_name} for {todo_id}: {outcome.reason}",
        )
    return outcome
