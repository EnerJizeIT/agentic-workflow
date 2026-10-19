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

R-03-F1 (TODO-0093): after a successful isolated commit the real index is
synced for the plan's files — otherwise it stays on the pre-commit base
and the next unit's plan subtracts the unit's own committed files as
"foreign" (A-17). R2.5: that sync moved into the resumable
publication protocol (``awf.publish._sync_owned_index``), which verifies
each entry's captured content before re-pointing it, so a foreign or
user-changed entry survives a reconciliation; this module keeps only the
gate that delegates the commit to it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import commit_delta, commit_plan, git_utils, paths, publish
from ._log import log as _log
from .infra import managed_fs

# R2.3: the delta-merge algorithm, the per-file delta staging and the
# isolated-index git runner live in the DeltaAssembler module now (one home).
# Re-exported under the legacy private names so existing importers — and tests
# that call ``commit_gate._apply_unit_delta`` directly — keep working unchanged.
_parse_delta_hunks = commit_delta.parse_delta_hunks
_head_position = commit_delta.head_position
_head_end_position = commit_delta.head_end_position
_apply_unit_delta = commit_delta.apply_unit_delta
#: One "is there a HEAD yet" rule for the whole commit path (REVIEW: the gate
#: and the assembler shared a byte-identical copy before — one home, no twins).
_has_head = commit_delta._has_head
#: One isolated-index git runner for the whole commit path (commit_delta owns
#: it; the gate's own commit/diff calls share it instead of a second copy).
_git_isolated = commit_delta._git_isolated

APPROVE_POLL_INTERVAL: int = 2

#: TODO-0195: the refusal marker for a quiet-diff skip over a NON-EMPTY
#: plan (the unit's work reached HEAD outside the unit — catch-up commit,
#: or HEAD moved since the baseline). The audit file and the RUN-REPORT
#: key off this prefix.
_PLAN_SKIP_INCONSISTENT = "plan-skip-inconsistent"


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


# REPORTS26 F3 (TODO-0163): the unit commit message. The legacy subject
# ``awf(<stage>): TODO-NNNN`` left ``git log`` blind — no essence, no files.
# Now the subject carries the TODO's title (≤ _SUBJECT_MAX_LEN total) and
# the body carries the full title, the unit id, and the files line.
_SUBJECT_MAX_LEN = 72


def _todo_title(project_dir: Path, todo_id: str) -> str:
    """The first H1 heading of ``inbox/TODO-<id>.md`` → title, or "".

    The dispatch boilerplate is stripped: the leading ``#``, the leading
    ``TODO(-id)`` token, and the separator run (``—``, ``–``, ``-``,
    ``:``, ``·``, ``•``, whitespace — any combination, any repetition).
    REPORTS33 (TODO-0204): the bullet (``·``) is stripped too — a heading
    ``# TODO-0001 — · Text`` must not yield the ``— ·`` subject artifact.
    A heading that is the boilerplate only (``# TODO-0001``) yields "" —
    the caller degrades to the legacy subject.
    """
    todo_file = paths.inbox(project_dir) / f"{todo_id}.md"
    try:
        text = todo_file.read_text(encoding="utf-8")
    except OSError:
        return ""
    for line in text.splitlines():
        m = re.match(r"^#\s+(.+)$", line)
        if not m:
            continue
        title = m.group(1).strip()
        title = re.sub(r"^TODO(?:-\d+)?\b", "", title)
        title = title.lstrip("—–-: \t·•")
        return title.strip()
    return ""


def _commit_subject(stage_name: str, todo_id: str, title: str) -> str:
    """``awf(<stage>): TODO-NNNN — <title>`` ≤ 72 chars, or the legacy
    subject when there is no title (or the title cannot fit at all)."""
    base = f"awf({stage_name}): {todo_id}"
    if not title:
        return base
    subject = f"{base} — {title}"
    if len(subject) <= _SUBJECT_MAX_LEN:
        return subject
    budget = _SUBJECT_MAX_LEN - len(base) - 3  # the " — " separator
    if budget <= 1:
        return base
    return f"{base} — {title[: budget - 1]}…"


def build_commit_message(
    stage_name: str,
    todo_id: str,
    project_dir: Path,
    plan: commit_plan.CommitPlan | None = None,
) -> str:
    """The unit commit message (REPORTS26 F3, TODO-0163).

    subject = ``awf(<stage>): TODO-NNNN — <title>`` (≤ 72 chars — the
    title is truncated, never the prefix), or the legacy
    ``awf(<stage>): TODO-NNNN`` when there is no title. body = the full
    title + ``unit: TODO-NNNN`` + the files line — ``plan.files``, the
    authoritative set the commit actually contains (REVIEW P2: the tree
    state is NOT the commit content — foreign staged changes live in the
    tree but outside the plan). Relative paths, the plan's stable order;
    absent without a plan or with an empty file set.
    """
    title = _todo_title(project_dir, todo_id)
    subject = _commit_subject(stage_name, todo_id, title)
    if not title:
        return subject
    body = [title, f"unit: {todo_id}"]
    if plan is not None and plan.files:
        body.append("files: " + ", ".join(plan.files))
    return subject + "\n\n" + "\n".join(body)


def _short_paths(paths_list: list[str], cap: int = 10) -> str:
    """The first ``cap`` paths for a refusal/audit message (… when more)."""
    shown = paths_list[:cap]
    more = len(paths_list) - len(shown)
    return ", ".join(shown) + (f" …({more} more)" if more else "")


def _unit_already_committed(
    project_dir: Path, baseline_sha: str, todo_id: str
) -> bool:
    """TODO-0195: does HEAD already carry the unit's OWN commit?

    The unit commit's subject is ``awf(<stage>): TODO-NNNN`` or
    ``awf(<stage>): TODO-NNNN — <title>`` (REPORTS26 F3) — so the scan of
    the ``baseline..HEAD`` subjects matches that FORMAT, not a bare id
    mention (REVIEW-F2): a catch-up like «chore: pick up TODO-NNNN
    leftovers» only MENTIONS the id and must not be mistaken for the unit's
    own commit. This keeps the double-commit pipeline shape (the execute
    stage committed, the verify stage re-runs the gate on the same tree —
    the quiet diff is then honest, the unit's own commit is in HEAD) on the
    legal-skip path. False (no unit-format commit in the range) makes the
    non-empty-plan quiet diff a REFUSAL instead of a silent archive — the
    0068 case: the work reached HEAD in someone else's catch-up commit and
    the unit was archived without its commit.
    """
    if not baseline_sha or not todo_id:
        return False
    try:
        # R2.5 architect-review: through the ONE runner the commit path
        # already owns (``commit_delta``) instead of a second raw launch —
        # the same cwd/timeout/capture contract, one place (AUD04-11).
        result = _git_isolated(
            project_dir, ["log", "--format=%s", f"{baseline_sha}..HEAD"]
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if result.returncode != 0:
        return False
    # REVIEW-F2: the unit's own commit is recognized by its OWN format
    # (REPORTS26 F3), not by a bare id mention — re.match pins the start,
    # (\\s|$) keeps the id boundary (TODO-0001 is not TODO-00012).
    own_format = re.compile(rf"awf\([^)]*\):\s*{re.escape(todo_id)}(\s|$)")
    return any(
        own_format.match(line) for line in result.stdout.splitlines() if line.strip()
    )


def _commit_via_isolated_index(
    project_dir: Path,
    plan: commit_plan.CommitPlan,
    message: str,
    logs_dir: Path | None = None,
    *,
    crash_at: str = "",
) -> commit_plan.CommitOutcome:
    """R-03: commit EXACTLY ``plan.files`` through a throwaway index.

    The isolated index is built from HEAD (when commits exist) plus the
    plan's files and deleted in ``finally``. R2.5: the commit object, the
    CAS ref move, the owned-index sync and the receipt are the resumable
    protocol in :mod:`awf.publish`. The sync re-points only the plan files
    whose real-index entry still equals the captured one (R-03-F1 — the
    next unit's plan then does not subtract its own committed files as
    "foreign"); a foreign or user-changed entry is left untouched and the
    receipt records ``committed_with_reconciliation_required``. On refusal
    or error the index is byte-identical (nothing was ever staged into it).
    """
    fd, index_path = tempfile.mkstemp(
        prefix=f"awf-commit-index-{plan.todo_id}-", suffix=".idx"
    )
    os.close(fd)
    env = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        try:
            # R2.3: the ONE staging home — read-tree, plain add, and the
            # per-file unit delta (REPORTS33) live in the DeltaAssembler
            # module. The isolated index is initialised there too (a 0-byte
            # file is not a valid index for git).
            staged = commit_delta.stage_plan_into_index(
                project_dir,
                env,
                list(plan.files),
                list(plan.delta_files),
                plan.todo_id,
                logs_dir,
            )
            if staged:
                return commit_plan.CommitOutcome(commit_plan.OUTCOME_ERROR, staged)
            if _has_head(project_dir):
                diff = _git_isolated(project_dir, ["diff", "--cached", "--quiet"], env)
                if diff.returncode == 0:
                    if not plan.files:
                        return commit_plan.CommitOutcome(
                            commit_plan.OUTCOME_SKIPPED,
                            "the plan's files match HEAD — nothing to commit",
                        )
                    # TODO-0195 (рана G): a NON-EMPTY plan whose files all
                    # match HEAD means the unit's work reached HEAD — but
                    # WHOSE commit carries it is the question. The unit's
                    # own earlier commit (the double-commit pipeline shape)
                    # is an honest skip; anything else (a catch-up commit,
                    # a moved HEAD) must not archive the unit silently.
                    if _unit_already_committed(
                        project_dir, plan.baseline_sha, plan.todo_id
                    ):
                        return commit_plan.CommitOutcome(
                            commit_plan.OUTCOME_SKIPPED,
                            "the plan's files match HEAD and the unit's own "
                            "commit is already in HEAD (an earlier stage of "
                            "this unit committed them) — nothing left to "
                            "commit",
                        )
                    return commit_plan.CommitOutcome(
                        commit_plan.OUTCOME_REFUSED,
                        f"{_PLAN_SKIP_INCONSISTENT}: the plan's file set is "
                        f"not empty ({len(plan.files)} file(s): "
                        f"{_short_paths(list(plan.files))}) but every plan "
                        "file matches HEAD and no unit commit is in "
                        "baseline..HEAD — the unit's work reached HEAD "
                        "outside the unit (a catch-up commit, or HEAD moved "
                        "since the baseline). The silent archive is "
                        "refused: the TODO stays active. Inspect "
                        "`git log` (baseline..HEAD) for the plan's files, "
                        "then re-verify (or re-dispatch when the work is "
                        "genuinely missing).",
                    )
            # R2.5: the commit object, the intent, the CAS ref move, the
            # owned-index sync and the receipt are one resumable protocol
            # (``awf.publish``). ``env`` carries the already-staged isolated
            # index; the user's real index is only touched by the verified
            # owned-sync inside it.
            outcome = publish.publish_from_env(
                project_dir, plan, env, message, logs_dir, crash_at=crash_at
            )
        except publish.PublishError as e:
            # D3 (R2.5 REVIEW, iter 5) + architector-review: a publication fact
            # (intent/receipt/...) could not be written — ``publish._write_json``
            # is the ONE writer and types BOTH the managed no-follow barrier
            # (``ManagedPathError``) and a generic I/O refusal as ``PublishError``.
            # Surface it as a typed outcome instead of letting a bare exception
            # crash the stage. The message lives in ``e`` (it names the actual
            # cause); the ref may already carry the unit's commit — a later
            # publish resumes.
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR,
                f"publication fact refused (the ref may already carry the "
                f"unit's commit; a later publish resumes safely): {e}",
            )
        except subprocess.SubprocessError as e:
            # L2 (R2.5 REVIEW, iter 4): a git LAUNCH failure (timeout / the
            # binary is missing) — this is honestly "the git command failed".
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR, f"git command failed: {e}"
            )
        except OSError as e:
            # L2: an OSError here is NOT "the git commit failed" — the git
            # steps report their own return codes. After the architector-review
            # the fact writes type their own refusals (``PublishError``), so
            # this branch is the remaining publication I/O: a git LAUNCH failure
            # (``FileNotFoundError``) or a tree read. The ref may already carry
            # the unit's commit, so name that instead of a false git diagnosis:
            # a later cycle resumes safely (the pending intent is on disk).
            return commit_plan.CommitOutcome(
                commit_plan.OUTCOME_ERROR,
                f"publication I/O failed (the ref may already carry the "
                f"unit's commit; a later publish resumes safely): {e}",
            )
        return outcome
    finally:
        try:
            os.unlink(index_path)
        except OSError:
            pass


def _write_commit_audit(
    project_dir: Path,
    plan: commit_plan.CommitPlan,
    outcome: commit_plan.CommitOutcome,
    logs_dir: Path,
) -> None:
    """TODO-0195: ``context/COMMIT-AUDIT-{todo}.json`` — the per-unit fact
    the RUN-REPORT renders («вне коммита остались»): the paths left outside
    the commit with their reasons, the worker-staged files (included,
    warned), and the refused silent skip (non-empty plan, files match
    HEAD). Best effort: a failed write logs and moves on — the audit is a
    trace, not a gate.
    """
    if not plan.todo_id:
        return
    skip_inconsistent = (
        outcome.status == commit_plan.OUTCOME_REFUSED
        and outcome.reason.startswith(_PLAN_SKIP_INCONSISTENT)
    )
    _write_commit_audit_record(
        project_dir,
        plan.todo_id,
        _audit_record(
            plan.todo_id,
            outcome,
            excluded=plan.excluded,
            worker_staged=plan.worker_staged,
            skip_inconsistent=skip_inconsistent,
        ),
        logs_dir,
    )


def _write_resume_audit(
    project_dir: Path,
    todo_id: str,
    outcome: commit_plan.CommitOutcome,
    logs_dir: Path,
) -> None:
    """R2.5 REVIEW (M1): the resume path reaches a committed unit BEFORE the
    plan is built, so it must still leave the per-unit audit fact — the
    TODO-0195 invariant «any post-plan outcome leaves the audit» covers a
    resumed publish too. The plan's excluded/worker-staged detail is not
    re-derived here (the resume builds no plan): the record carries the
    outcome status, the commit OID and a ``resumed`` mark, which the
    RUN-REPORT renders as the unit's commit line.
    """
    if not todo_id:
        return
    _write_commit_audit_record(
        project_dir,
        todo_id,
        _audit_record(todo_id, outcome, resumed=True),
        logs_dir,
    )


def _audit_record(
    todo_id: str,
    outcome: commit_plan.CommitOutcome,
    *,
    excluded: list | tuple = (),
    worker_staged: list | tuple = (),
    skip_inconsistent: bool = False,
    resumed: bool = False,
) -> dict:
    """The ONE shape of ``COMMIT-AUDIT-{todo}.json``.

    Two callers fill it: the plan path (excluded/worker-staged detail from
    the plan) and the resume path (no plan — the outcome alone). Before this
    the same keys were spelled as two literals next to each other, so a new
    field had to be added in both or the RUN-REPORT readers would drift
    (R2.5 architect-review). ``resumed`` and ``commit`` appear only on the
    resume record: the commit OID has ONE home in status / RUN-REPORT
    (``publish.publication_facts``), and the plan path keeps its historical
    shape. The resume path needs it in the audit because it runs before any
    plan is built.
    """
    record = {
        "todo_id": todo_id,
        "status": outcome.status,
        "excluded": [[p, r] for p, r in excluded],
        "worker_staged": list(worker_staged),
        "skip_inconsistent": skip_inconsistent,
        "skip_reason": outcome.reason if skip_inconsistent else "",
    }
    if resumed:
        record["commit"] = outcome.sha
        record["resumed"] = True
    return record


def _write_commit_audit_record(
    project_dir: Path,
    todo_id: str,
    record: dict,
    logs_dir: Path,
) -> None:
    """Best-effort write of the audit record (a trace, not a gate).

    R2.5 architector-review: this fact lives in ``.agentic/context`` and is
    publication state like the intent/receipt, so it leaves through the SAME
    managed no-follow writer (R1 carry a) — a symlinked ``.agentic`` /
    ``context`` is refused instead of the audit landing outside the project.
    The write stays best-effort: a barrier refusal is logged, not raised."""
    path = paths.context_dir(project_dir) / f"COMMIT-AUDIT-{todo_id}.json"
    try:
        managed_fs.managed_write_text(
            path,
            publish.dumps_fact(record),
            root=managed_fs.agentic_root_for(path.parent),
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(logs_dir, f"TODO-0195: commit audit for {todo_id} not written: {e}")


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
    wait     window writes VERIFIED-{todo}.sha after the build, and its
    fingerprint must be enforced, not silently skipped (REVIEW-0087 P2).
    Both are plan checks — there is no separate duplicate check in the
    commit path.

    M2.1: in an active run the approval is checked too — the APPROVE
    signal carries the binding (run generation, verified fingerprint,
    file-set digest) and any mismatch refuses the commit
    (``commit_plan.binding_refusal``).
    """
    # R2.5: a crash mid-publication is resumable. A pending intent is keyed
    # on the TODO and the exact commit OID, so it is resumed BEFORE any
    # policy/verdict/fingerprint check — the ref may already carry the unit's
    # own commit (whose approval was consumed), and re-running the checks on
    # a moved HEAD would wrongly refuse. Resume creates NO second commit.
    # The resume itself is fully typed (``publish.resume_if_pending`` owns the
    # git/IO boundary and returns a typed outcome, including ERROR) — the
    # stage does not re-wrap it here: the boundary lives IN the protocol, and
    # the per-unit audit below covers both success and failure.
    pending = publish.resume_if_pending(project_dir, todo_id, logs_dir)
    if pending is not None:
        _log(
            logs_dir,
            f"R2.5: resumed pending publication for {todo_id}: "
            f"{pending.status} {pending.reason}".strip(),
        )
        # R2.5 REVIEW (M1): a resumed publish is a post-plan outcome too —
        # leave the per-unit audit fact so the RUN-REPORT shows the unit's
        # commit instead of silently dropping the section (TODO-0195).
        _write_resume_audit(project_dir, todo_id, pending, logs_dir)
        return pending

    if policy not in ("commit_and_next", "commit_and_report"):
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_SKIPPED, f"policy {policy!r} does not commit"
        )

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return commit_plan.CommitOutcome(commit_plan.OUTCOME_SKIPPED, "not a git repo")

    plan = commit_plan.build_commit_plan(project_dir, todo_id, baseline_sha)

    # TODO-0195: every post-plan outcome (refused/skipped/committed/error)
    # leaves the per-unit audit fact for the RUN-REPORT. The closure reads
    # ``plan`` at call time, so the auto-mode refresh below is covered.
    def _finalize(outcome: commit_plan.CommitOutcome) -> commit_plan.CommitOutcome:
        _write_commit_audit(project_dir, plan, outcome, logs_dir)
        return outcome

    # R2.3: a model refusal (unsupported gitlink / unmerged / sparse / split
    # index, or a mixed-ownership rename pair) blocks the plan — refuse
    # explicitly, never a silent legacy commit.
    if plan.refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {plan.refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"R2.3: commit refused for {todo_id} — {plan.refusal}")
        return _finalize(commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, plan.refusal))

    refusal = commit_plan.verdict_refusal(plan, project_dir)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"A-04: commit refused for {todo_id} — {refusal}")
        return _finalize(commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal))

    # BD-17: accept either APPROVE-{todo}.ready (from `awf approve` /
    # human) or ACK-{todo}.ready (from supervisor verify subprocess in
    # auto mode). In an active run only the bound APPROVE authorizes the
    # commit (M2.1 binding_refusal) — `which` is what the binding check
    # sees.
    inbox = paths.inbox(project_dir)
    approve_signal = inbox / f"APPROVE-{todo_id}.ready"
    ack_signal = inbox / f"ACK-{todo_id}.ready"

    if auto:
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
    else:
        which = "APPROVE" if approve_signal.exists() else (
            "ACK" if ack_signal.exists() else ""
        )

    refusal = commit_plan.fingerprint_refusal(plan, project_dir)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"A-15: commit refused for {todo_id} — {refusal}")
        return _finalize(commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal))

    # M2.1: the approval's binding — which run generation it was made in,
    # and the file set it covered. Runs after A-15 so a moved tree still
    # gets the (more actionable) re-verify message first.
    refusal = commit_plan.binding_refusal(plan, project_dir, which)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"M2.1: commit refused for {todo_id} — {refusal}")
        return _finalize(commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal))

    # R2.4: the approval's candidate binding — the run generation and the
    # file set are pinned by M2.1, but the CANDIDATE (its tree, contract,
    # environment, degraded paths) must still describe the tree the gate
    # will commit. A moved HEAD, a contract change or a content change
    # invalidates the evidence → refuse (HEAD moved means REPLAN).
    refusal = commit_plan.candidate_refusal(plan, project_dir)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"R2.4: commit refused for {todo_id} — {refusal}")
        return _finalize(commit_plan.CommitOutcome(commit_plan.OUTCOME_REFUSED, refusal))

    if not plan.files:
        print(f"No changes to commit — skip commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"A1: no diff vs baseline at {stage_name}")
        # R2.5: a unit that legitimately produced no code records a no-code
        # receipt (with its own evidence) so the archive gate has a fact to
        # stand on. An empty plan WITHOUT evidence is NOT laundered into a
        # no-code outcome — no receipt is written and the archive refuses.
        try:
            publish.record_no_code_receipt(project_dir, todo_id)
        except (publish.PublishError, OSError) as e:
            _log(logs_dir, f"R2.5: no-code receipt not recorded for {todo_id}: {e}")
        return _finalize(
            commit_plan.CommitOutcome(
                commit_plan.OUTCOME_SKIPPED, commit_plan.NO_CHANGES_REASON
            )
        )

    # TODO-0195: the worker staged files AFTER the baseline (a hard-rule
    # violation) — its own work, so it JOINS the unit commit; the warning
    # is the loud part (stderr + log), not a refusal.
    if plan.worker_staged:
        worker_warning = (
            f"worker staged {len(plan.worker_staged)} file(s) after the "
            f"baseline (the unit must not stage — hard rule): "
            f"{_short_paths(list(plan.worker_staged))} — included in the "
            "unit commit"
        )
        print(f"WARNING: {worker_warning}", file=sys.stderr)
        _log(logs_dir, f"TODO-0195: {worker_warning}")

    outcome = _commit_via_isolated_index(
        project_dir,
        plan,
        build_commit_message(stage_name, todo_id, project_dir, plan),
        logs_dir=logs_dir,
    )
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
    return _finalize(outcome)
