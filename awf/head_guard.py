"""A31: the engine notices a commit made outside the commit gate.

The commit gate (``awf.commit_gate``) is the only writer of unit commits.
A supervisor subprocess that runs ``git add -A && git commit`` on its own
produces a commit the engine never saw: no ``awf(...)`` subject, no unit
archiving, and the TODO stays active (incident TODO-0323, 01.11).

The engine therefore snapshots HEAD at each stage start, lets the gate
record the commit it made (``record_gate_commit``), and compares the two
after the stage (``check_stage_head``). A moved HEAD that the gate cannot
account for is a FOREIGN COMMIT: a loud error with the SHAs and the file
list, a line in the run log, the fact in ``awf status``, and the run stops
instead of continuing silently.

A39 (incident TODO-0340): not every move outside the gate is foreign. The
supervisor landing an upstream branch — a fast-forward of local ``main``
onto ``origin/main``, or a merge whose first parent is the stage's HEAD and
whose other parent is an upstream ref — is an external update. The guard
warns in the log, records ``upstream_sync`` for ``awf status``, and lets
the run continue. Any other move stays FOREIGN.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import git_utils, pipeline_state
from ._log import log as _log
from .infra.git_model import GitModelError, GitRepository, display_path

#: State keys: the gate's last commit and its parent (the HEAD it committed
#: on). Reset at every stage start; written only by ``record_gate_commit``.
GATE_HEAD_KEY = "gate_head_sha"
GATE_PARENT_KEY = "gate_head_parent"
#: The human-readable FOREIGN COMMIT line, surfaced by ``awf status``.
FOREIGN_COMMIT_KEY = "foreign_commit"
#: A39: an external HEAD move that is NOT foreign — the supervisor landed an
#: upstream branch (``git pull``/fast-forward). The run continues; the line is
#: the informational counterpart of FOREIGN COMMIT in ``awf status``.
UPSTREAM_SYNC_KEY = "upstream_sync"
#: Refs under these namespaces count as upstream. A local branch is not:
#: a foreign commit reaches HEAD through the worktree, never through a
#: remote-tracking ref the engine did not see arrive.
UPSTREAM_REF_PREFIXES = ("refs/remotes/origin/",)


def head_sha(project_dir: str | Path) -> str:
    """Return HEAD's SHA, or "" for a missing repo / unborn HEAD."""
    try:
        return git_utils.current_sha(project_dir)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""


def _parent_sha(project_dir: Path, sha: str) -> str:
    """Return the parent of ``sha``, or "" when it has none / cannot be read."""
    if not sha:
        return ""
    try:
        return git_utils.git_stdout(project_dir, "rev-parse", f"{sha}^").strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""


def _is_ancestor(project_dir: Path, ancestor: str, descendant: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``."""
    if not ancestor or not descendant:
        return False
    try:
        git_utils.git_stdout(
            project_dir, "merge-base", "--is-ancestor", ancestor, descendant
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    return True


def _upstream_ref_containing(project_dir: Path, sha: str) -> str:
    """The first upstream-tracking ref that contains ``sha``, or ``""``.

    One ``for-each-ref --contains`` read instead of a per-ref loop: the
    ref list is the repository's, not the engine's, and it can be long.
    """
    if not sha:
        return ""
    try:
        out = git_utils.git_stdout(
            project_dir,
            "for-each-ref",
            f"--contains={sha}",
            "--format=%(refname)",
            *UPSTREAM_REF_PREFIXES,
        )
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return ""
    for line in out.splitlines():
        name = line.strip()
        # ``refs/remotes/origin/HEAD`` is a symbolic alias, not a branch: it
        # sorts first and would name the alias instead of ``origin/main`` in
        # the line the owner reads.
        if name and not name.endswith("/HEAD"):
            return name
    return ""


def _parents(project_dir: Path, sha: str) -> list[str]:
    """The parent SHAs of ``sha`` in order (empty for a root/unreadable one)."""
    if not sha:
        return []
    try:
        out = git_utils.git_stdout(project_dir, "rev-list", "--parents", "-n", "1", sha)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return []
    return out.split()[1:]


def upstream_sync(project_dir: Path, before: str, after: str) -> str:
    """Classify a moved HEAD as an upstream update; ``""`` when it is not one.

    The two legitimate shapes (incident TODO-0340: the supervisor landed PR
    #86, local ``main`` fast-forwarded onto ``origin/main``):

    * **fast-forward** — ``before`` is an ancestor of ``after`` and ``after``
      already lives in an upstream-tracking ref;
    * **merge** — the first parent of the merge commit is ``before`` and
      another parent lives in an upstream-tracking ref.

    Anything else stays a candidate for FOREIGN: a local commit is neither
    an ancestor of the old HEAD nor reachable from a remote-tracking ref,
    and a merge whose first parent is not the old HEAD moved the branch
    under the engine's feet.
    """
    if not before or not after or before == after:
        return ""
    if _is_ancestor(project_dir, before, after):
        ref = _upstream_ref_containing(project_dir, after)
        if ref:
            return f"fast-forward to {ref}"
    parents = _parents(project_dir, after)
    if len(parents) >= 2 and parents[0] == before:
        for parent in parents[1:]:
            ref = _upstream_ref_containing(project_dir, parent)
            if ref:
                return f"merge of {ref}"
    return ""


def changed_files(project_dir: Path, before: str, after: str) -> list[str]:
    """Files touched between two commits — the evidence in the error line.

    The read goes through the byte-faithful model (``-z``), not a printed
    ``--name-only`` listing: a path is a fact with ONE owner, and a
    non-UTF-8 name must reach the line as itself (caller-visible text).
    """
    if not before or not after:
        return []
    try:
        raw = GitRepository(project_dir).paths_between(before, after)
    except (GitModelError, OSError):
        return []
    return [display_path(path) for path in raw]


def _resolve_commit(project_dir: Path, sha: str) -> str:
    """Return the full OID for ``sha`` (full, short or ``HEAD``).

    A31 QA: the gate's outcome SHA is the SHORT form (``publish._short`` —
    the one printed as "Auto-committed … (abc1234)"), while HEAD is read as
    a full OID. The drift check compares the two, so both sides must live in
    one form — otherwise every legitimate gate commit looks foreign. The
    caller-visible short form stays untouched; only the stored fact is
    normalized.
    """
    if not sha:
        return ""
    try:
        resolved = git_utils.git_stdout(
            project_dir, "rev-parse", "--verify", f"{sha}^{{commit}}"
        ).strip()
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return sha
    return resolved or sha


#: The one sentence both advice surfaces append. A literal copy in each
#: function would be the second owner of the same fact and could drift.
_DO_NOT_RESUME = "Do not just resume — it erases the reason."


def foreign_commit_next_action() -> str:
    """The recovery step for a FOREIGN COMMIT, short enough for the card.

    Shared by the two advice surfaces (``awf.brief`` next action and the
    wait-event wake-up) so they cannot drift apart: one fact, one owner.
    The card clips ``next:`` at ``brief._NEXT_ACTION_MAX_WORDS`` (26), so
    the action must not carry the evidence line: a long SHA/file list in
    front would cut the recovery sentence off. The evidence stays in
    ``awf status`` and the run log.

    A foreign commit cannot be "recommitted through the gate" in place:
    the gate refuses ``plan-skip-inconsistent`` on a moved tree. The
    working path is rollback and a replay of the unit.
    """
    return (
        "FOREIGN COMMIT: inspect `git log -1`, `awf rollback`, then replay "
        f"the unit through the gate. {_DO_NOT_RESUME}"
    )


def foreign_commit_hint(line: str) -> str:
    """The stored fact plus the one thing the line itself does not say.

    The line already tells the reader to inspect ``git log`` and recover by
    hand; repeating that would be the second wording of the same step. The
    extra fact is that a plain resume erases the evidence.
    """
    return f"{line} {_DO_NOT_RESUME}"


def record_gate_commit(
    project_dir: Path, sha: str, logs_dir: Path | None = None
) -> None:
    """Record the commit the gate just made, so the drift check accepts it."""
    full_sha = _resolve_commit(project_dir, sha)
    if not full_sha:
        return
    pipeline_state.write_state(
        project_dir,
        logs_dir=logs_dir,
        **{
            GATE_HEAD_KEY: full_sha,
            GATE_PARENT_KEY: _parent_sha(project_dir, full_sha),
        },
    )


def reset_stage_markers(project_dir: Path, logs_dir: Path | None = None) -> None:
    """Clear the per-stage gate markers at the stage boundary.

    The FOREIGN COMMIT line is deliberately NOT cleared here: it is the
    evidence of a run-stopping fact and must survive a resume until a clean
    stage proves recovery (otherwise ``awf continue`` would erase the reason
    before the next stage even ran). :func:`check_stage_head` clears it.
    """
    pipeline_state.write_state(
        project_dir,
        logs_dir=logs_dir,
        **{GATE_HEAD_KEY: None, GATE_PARENT_KEY: None},
    )


def _clear_head_marks(project_dir: Path, logs_dir: Path | None = None) -> None:
    """Drop the per-boundary HEAD lines once a stage boundary came back clean.

    Both facts describe a move that the boundary accounted for, so neither
    outlives it: FOREIGN is the evidence of a stop (kept until a clean stage
    proves recovery), UPSTREAM SYNC is the informational counterpart (kept
    until the same proof). Without the second key here the sync line would
    stay in ``awf status`` for the rest of the run and read as current.
    """
    state = pipeline_state.read_state(project_dir) or {}
    stale = {
        key: None
        for key in (FOREIGN_COMMIT_KEY, UPSTREAM_SYNC_KEY)
        if state.get(key)
    }
    if stale:
        pipeline_state.write_state(project_dir, logs_dir=logs_dir, **stale)


def check_stage_head(
    project_dir: Path,
    head_before: str,
    todo_id: str,
    stage_name: str,
    logs_dir: Path | None = None,
) -> str | None:
    """Return the FOREIGN COMMIT line when HEAD moved outside the gate.

    A moved HEAD is legitimate only when the gate committed it: the new
    HEAD equals the recorded gate commit AND that commit sits directly on
    the HEAD the stage started from (the ``parent`` check catches a foreign
    commit that the gate then committed on top of).

    A39: an upstream landing (fast-forward/merge from a remote-tracking
    ref, see :func:`upstream_sync`) is the other legitimate move — the line
    is informational, the run continues.
    """
    head_after = head_sha(project_dir)
    if not head_before or not head_after:
        return None
    if head_after == head_before:
        _clear_head_marks(project_dir, logs_dir)
        return None

    state = pipeline_state.read_state(project_dir) or {}
    gate_sha = _resolve_commit(
        project_dir, str(state.get(GATE_HEAD_KEY) or "")
    )
    gate_parent = str(state.get(GATE_PARENT_KEY) or "")
    if gate_sha == head_after and gate_parent == head_before:
        _clear_head_marks(project_dir, logs_dir)
        return None

    # A39: the supervisor landing an upstream branch moves HEAD without the
    # gate — legitimately. The shape decides: fast-forward/merge from a
    # remote-tracking ref is an external update (warn, keep the run going);
    # any other move stays FOREIGN.
    #
    # The landing and the gate's commit can share ONE stage (the supervisor
    # pulls, then approves; the gate commits on top of the synced HEAD). Then
    # the gate's record names the synced tip, not the stage's HEAD, and the
    # move still has to be classified — against the gate's parent.
    sync = ""
    gate_suffix = ""
    if gate_sha == head_after and gate_parent and gate_parent != head_before:
        sync = upstream_sync(project_dir, head_before, gate_parent)
        if sync:
            gate_suffix = f", then the gate committed {head_after[:12]}"
    if not sync:
        sync = upstream_sync(project_dir, head_before, head_after)
    # The guard covers every stage, not only the supervisor's: the line must
    # not blame a role it cannot identify. The unit is the label the stage
    # started with; the plan stage may still have none.
    unit = todo_id or "no unit yet"
    if sync:
        cause = (
            f"{sync}{gate_suffix}; the run continues."
            if gate_suffix
            else f"{sync}, not made by the commit gate. The run continues."
        )
        line = (
            f"UPSTREAM SYNC: HEAD moved during stage '{stage_name}' ({unit}) "
            f"{head_before[:12]}..{head_after[:12]} — {cause}"
        )
        print(f"WARNING: {line}", file=sys.stderr)
        _log(logs_dir, line)
        pipeline_state.write_state(
            project_dir, logs_dir=logs_dir, **{UPSTREAM_SYNC_KEY: line}
        )
        return None

    files = changed_files(project_dir, head_before, head_after)
    listed = ", ".join(files) if files else "(no file list — git diff failed)"
    line = (
        f"FOREIGN COMMIT: HEAD moved during stage '{stage_name}' "
        f"({unit}) {head_before[:12]}..{head_after[:12]} — not made by "
        f"the commit gate. Files: {listed}. The run stops: inspect "
        f"`git log -1 {head_after[:12]}` and recover the unit by hand "
        f"(no role commits — the gate does)."
    )
    print(f"ERROR: {line}", file=sys.stderr)
    _log(logs_dir, line)
    pipeline_state.write_state(
        project_dir,
        logs_dir=logs_dir,
        **{FOREIGN_COMMIT_KEY: line, UPSTREAM_SYNC_KEY: None},
    )
    return line
