"""TODO dispatch: atomic create + baseline + ready signal.

Replaces the 3-step manual workflow:
  1. Write inbox/TODO-NNNN.md
  2. awf_baseline TODO-NNNN (snapshot)
  3. touch inbox/TODO-NNNN.ready (signal)

With one call. Auto-picks next NNNN, creates baseline snapshot, dispatches
the signal. Supervisor gets back the todo_id + baseline_sha to track.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from .. import git_utils, paths
from ..dispatch_policy import (
    ISOLATE,
    REQUIRE_CLEAN,
    SourceTreeReport,
    check_source_tree,
    format_dirty_refusal,
    format_isolate_report,
    log_policy_decision,
    resolve_dirty_policy,
)
from ..infra import managed_fs
from ..todo_ids import is_valid_todo_id
from ._errors import AwfApiError
from ._helpers import require_awf_project
from ._results import DispatchTodoResult
from .pipeline import create_baseline

#: TODO-0195: the dirty-tree warning lists at most this many files
#: (the rest as "…N more") — the same cap convention as the untracked one.
_DIRTY_WARNING_CAP = 10

# REPORTS26 F5: a TODO number in the FIRST line's heading (a copied
# template keeps its old title). Only the first line counts — a
# TODO-NNNN deeper in the body is ordinary text about another unit.
_HEADING_TODO_NUM_RE = re.compile(r"^#{1,6}\s+TODO-(\d+)")


def _heading_todo_number(content: str) -> int | None:
    """The number from a ``TODO-NNNN`` heading on the first line, else None."""
    if not content:
        return None
    m = _HEADING_TODO_NUM_RE.match(content.splitlines()[0])
    return int(m.group(1)) if m else None


def _renumber_heading(content: str, todo_id: str) -> str:
    """Replace the number in the first line's heading with ``todo_id``'s.

    Only the first line is touched; the rest of the content stays byte-
    for-byte. The regex matched ``#... TODO-<digits>`` at the start of the
    line, so the first ``TODO-<digits>`` occurrence in it IS the heading's.
    """
    lines = content.splitlines(keepends=True)
    lines[0] = re.sub(r"TODO-\d+", todo_id, lines[0], count=1)
    return "".join(lines)


# TODO-0186: file extensions that mark a token as a path even without a
# separator. Deliberately short — the heuristic stays conservative
# ("in doubt, don't warn"), so only the extensions that actually appear in
# verify commands are listed.
_PATH_EXTS = (".py", ".sh", ".md", ".yaml", ".yml", ".j2")

#: Flags that take their value as the NEXT token (not ``--flag=value``). A
#: path-like token right after one of these is the flag's argument, not a
#: positional path — skipped (in doubt, don't warn).
_VALUE_TAKING_FLAGS = frozenset({
    "-k", "--keyword",
    "-m", "--marker",
    "-p", "--plug-in", "--plugin",
    "-c", "--config",
    "-W", "--pythonwarnings",
    "--timeout", "--rootdir", "--confcutdir",
    "--ds", "--durations", "--maxfail",
    "-n",
})


def _shlex_tokens(cmd: str) -> list[str]:
    """Shell-split a verify command, tolerating broken quoting.

    ``shlex.split`` handles the quote wrappers (``-k "stack or init"`` is
    one token, not fragments); a ValueError from unbalanced quotes falls
    back to a plain whitespace split (the heuristic is best-effort and must
    never raise).
    """
    try:
        return shlex.split(cmd, posix=True)
    except ValueError:
        return cmd.split()


def _looks_like_path(token: str, prev: str) -> bool:
    """Whether a verify-command token is a file-path candidate.

    A candidate contains ``/`` or ends with a known extension, and is not a
    flag (leading ``-``), a URL (``://``), a shell variable (``$``), or the
    separate value of a value-taking flag (``-k EXPR``). In doubt the token
    is NOT a path — the heuristic has no right to warn falsely.
    """
    if not token or token.startswith("-"):
        return False
    if "://" in token or "$" in token:
        return False
    if prev in _VALUE_TAKING_FLAGS:
        return False
    return "/" in token or token.endswith(_PATH_EXTS)


def _verify_path_tokens(commands: list[str]) -> list[str]:
    """Extract the path-like tokens from verify commands (TODO-0186).

    Runs the :func:`_looks_like_path` heuristic over the shell-split tokens
    of every command. Returns the unique candidates in first-seen order —
    the caller checks their existence relative to the project.
    """
    found: list[str] = []
    seen: set[str] = set()
    for cmd in commands:
        tokens = _shlex_tokens(cmd)
        prev = ""
        for token in tokens:
            if _looks_like_path(token, prev) and token not in seen:
                seen.add(token)
                found.append(token)
            prev = token
    return found


def _missing_verify_paths(project_dir: Path, tokens: list[str]) -> list[str]:
    """The path tokens that do NOT exist relative to ``project_dir``.

    A relative token is joined onto the project root; an absolute token
    keeps its own root (pathlib semantics).
    """
    return [t for t in tokens if not (project_dir / t).exists()]


def _dirty_tree_classes(project_dir: Path) -> dict[str, str]:
    """TODO-0195 / TODO-0204 / TODO-0221: the paths that are NOT clean at
    dispatch time, with their class: ``staged`` (the index differs from
    HEAD), ``tracked-dirty`` (a worktree-only change of a tracked path —
    the pre-dispatch WIP the delta mechanism covers), ``untracked``.

    Read through the R2.1 git model — ONE git parser, byte-faithful paths:
    the printed ``git status --short`` C-quotes a non-ASCII name
    (``"\\321\\204..."``), so an adopted path the model gives as raw bytes
    (``файл.txt``) no longer compared equal and the exclusion missed
    (TODO-0221 M1). First-seen order, duplicates dropped. ``{}`` on a clean
    tree,     and on a tree the model refuses too (best effort — this feeds a
    warning, it must never refuse a dispatch).

    The untracked side comes from :meth:`GitRepository.untracked_paths`, the
    SAME listing the policy check reads: ``changes()`` drops a secret-looking
    untracked name (``.env``) from the ownership operations, so the warning
    would omit a path the require_clean refusal / isolate table name — the
    three surfaces must describe one tree.
    """
    from ..dispatch_policy import is_managed_runtime_path
    from ..infra.git_model import GitModelError, GitRepository, display_path

    if not git_utils.is_git_repo(project_dir):
        return {}
    try:
        repo = GitRepository(project_dir)
        records = repo.changes()
        untracked = repo.untracked_paths()
    except GitModelError:
        return {}  # best effort: the warning must never refuse a dispatch
    classes: dict[str, str] = {}
    for r in records:
        # R2.2: managed AWF runtime artifacts are never the USER's dirt — the
        # same documented exceptions the source-tree policy check applies.
        if is_managed_runtime_path(r.path):
            continue
        path = display_path(r.path)
        if not path or path in classes:
            continue
        if r.kind == "untracked":
            classes[path] = "untracked"
        elif _change_is_staged(r):
            classes[path] = "staged"
        else:
            classes[path] = "tracked-dirty"
    for raw in untracked:
        if is_managed_runtime_path(raw):
            continue
        path = display_path(raw)
        if path:
            classes.setdefault(path, "untracked")
    return classes


def _change_is_staged(record) -> bool:
    """True when the R2.1 change sits in the INDEX (staged vs HEAD).

    The record's index facts moved from the base ones, or the path is NEW in
    the index (added/renamed/copied — the base has no entry). Everything
    else is a worktree-only change of a tracked path (``tracked-dirty``),
    the class the dispatch-time worktree snapshot and the delta mechanism
    cover.
    """
    if record.kind in ("added", "renamed", "copied"):
        return True
    return (
        record.index_blob != record.base_blob
        or record.index_mode != record.base_mode
    )


def _group_list(paths_list: list[str], cap: int = 5) -> str:
    """The first ``cap`` paths for a warning group (…N more when cut).

    Names render through :func:`escape_str_for_ui` (F3) — a non-UTF-8 byte in
    a path must not break the strict-UTF-8 sink the warning is emitted
    through.
    """
    from ..infra.git_model import escape_str_for_ui

    shown = [escape_str_for_ui(p) for p in paths_list[:cap]]
    more = len(paths_list) - len(shown)
    return ", ".join(shown) + (f" …({more} more)" if more else "")


def _format_dirty_warning(
    todo_id: str,
    files: list[str],
    classes: dict[str, str],
    snapshot_available: bool,
    snapshot_reason: str = "",
) -> str:
    """TODO-0195 / TODO-0204: the dispatch warning for a dirty tree —
    honest about what the commit gate will actually do. ``""`` when the
    tree is clean (nothing to say).

    Tracked-dirty files (the pre-dispatch WIP): with the dispatch-time
    worktree snapshot (``BASELINE-{todo}.worktree``) the gate commits
    only the unit's own edit — the pre-dispatch change STAYS OUTSIDE the
    unit commit. Without the snapshot (not a git repo, a failed or absent
    snapshot) the gate cannot split the two — the pre-dispatch change
    WILL ENTER the commit as-is, and the warning says so. Staged-at-
    baseline and untracked files never enter the commit (claim them via
    include_untracked / carry_over_from) — in every case.
    """
    if not files:
        return ""
    from ..infra.git_model import escape_str_for_ui

    shown = [escape_str_for_ui(f) for f in files[:_DIRTY_WARNING_CAP]]
    more = len(files) - len(shown)
    tail = f" (…{more} more)" if more else ""
    claim = (
        "staged-at-dispatch and untracked files do not enter the commit "
        "(to claim them: include_untracked / carry_over_from)."
    )
    tracked = [f for f in files if classes.get(f) == "tracked-dirty"]
    if not tracked:
        # Untracked/staged only — the legacy promise holds as-is.
        return (
            f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
            f"({', '.join(shown)}{tail}) will NOT enter the {todo_id} commit — "
            "these changes pre-date the unit. To claim them into the unit: "
            "include_untracked (untracked files) / carry_over_from (a rejected "
            "retry); otherwise they stay WIP for whoever commits them."
        )
    td = _group_list(tracked)
    if snapshot_available:
        return (
            f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
            f"({', '.join(shown)}{tail}). pre-dispatch changes to "
            f"tracked-dirty files ({td}) stay OUTSIDE the unit commit — "
            "only the unit's own edits to them will land; " + claim
        )
    reason = snapshot_reason or "no worktree snapshot available"
    return (
        f"⚠️ the tree is dirty at dispatch: {len(files)} file(s) "
        f"({', '.join(shown)}{tail}). pre-dispatch changes to "
        f"tracked-dirty files ({td}) WILL ENTER the unit commit as-is — "
        f"{reason}, so the commit gate cannot exclude them; " + claim
    )


def _next_todo_id(project_dir: Path) -> str:
    """Pick next TODO-NNNN id (max existing + 1).

    Scans inbox, outbox, AND done/ for any TODO-NNNN.* files to avoid
    collisions with already-completed/archived TODOs.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    done = paths.done_dir(project_dir)

    max_num = 0
    for d in (inbox, outbox, done):
        if not d.is_dir():
            continue
        for f in d.glob("TODO-*.md"):
            m = re.match(r"^TODO-(\d+)$", f.stem)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        # Also check done/ subdirectories (done/TODO-NNNN/)
        if d == done:
            for sub in d.iterdir():
                if sub.is_dir():
                    m = re.match(r"^TODO-(\d+)$", sub.name)
                    if m:
                        num = int(m.group(1))
                        if num > max_num:
                            max_num = num

    return f"TODO-{max_num + 1:04d}"


def dispatch_todo(
    project_dir: Path,
    content: str,
    *,
    role: str | None = None,
    todo_id: str | None = None,
    pipeline: str | None = None,
    carry_over_from: str | None = None,
    include_untracked: list[str] | None = None,
    allow_mismatch: bool = False,
    replan_for: str | None = None,
    claim_from: str | None = None,
) -> DispatchTodoResult:
    """Atomically create a TODO, baseline it, dispatch the signal.

    Three-step manual workflow compressed into one call:
      1. Write ``.agentic/inbox/TODO-NNNN.md`` (atomic).
      2. Create ``.agentic/context/BASELINE-NNNN.{sha,status,tests.log,env.log}``.
      3. Write ``.agentic/inbox/TODO-NNNN.ready`` signal (trigger for plan stage).

    Args:
        project_dir: awf project root (must contain .agentic/).
        content: TODO markdown body (the task description).
        role: optional role hint for logging/debugging (does not affect
            pipeline routing — that's determined by stage order in
            pipeline.yaml). Useful for supervisor context: "this TODO
            is for agent-system-analyst".
        todo_id: override auto-generated id (e.g. "TODO-0007"). If None,
            auto-picks next available NNNN by scanning inbox + outbox.
        pipeline: RUN3 #2 (Part B) — optional pipeline name, written into
            the TODO's contract block as ``pipeline: <name>`` (the block is
            created when absent). When an ``awf_run_next`` launch has no
            queue-level pipeline, it uses this one.
        carry_over_from: RUN5 #1 (leak-gate) — the id of a REJECTED TODO
            whose untracked files this retry re-claims. The paths recorded
            in ``.agentic/context/REJECT-<origin>.files`` are excluded from
            the new baseline's untracked snapshot, so the retry's commit
            includes them. Refused (before any side effect) when the origin
            TODO or its REJECT file does not exist.
        include_untracked: RUN10 #4 (TODO-0074) — pre-existing untracked
            files to include in this unit's commit. Each path must exist,
            be untracked, be not gitignored, and stay inside the project;
            ANY invalid path refuses the dispatch before any side effect
            (no TODO, no baseline). The paths are excluded from the
            baseline's untracked snapshot, so the commit gate commits them
            with the unit. The trace lives in
            ``.agentic/context/BASELINE-<id>.include``. Independently, the
            answer always lists the files that STAY excluded
            (``pre_existing_untracked`` + ``untracked_warning``).
        allow_mismatch: REPORTS26 F5 — heading/title mismatch policy. The
            first line of the content is a heading carrying a ``TODO-NNNN``
            number:

             - auto number (``todo_id`` is None) and the number differs from
               the issued one → the heading is renumbered to the issued id,
               the result carries ``renumbered=True`` (rest of the text
               untouched);
             - explicit ``todo_id`` and the number differs → the dispatch is
               refused BEFORE any side effect (the message names both
               numbers); ``allow_mismatch=True`` accepts the mismatch and
               writes the content as-is (``renumbered=False``).
        replan_for: TODO-0213 (BD-30) — optional marker that this TODO is
            the replan answer for the unit being replaced (its id). The
            marker ``replan_for: <id>`` is written into the TODO's contract
            block (created when absent; an explicit argument wins over a
            copy-pasted block, like ``pipeline=``). Only a TODO carrying the
            marker is picked up by the interactive replan wait — an
            ordinary mid-run dispatch (no marker) does not switch a live
            pipeline. The referenced unit must exist and be live: refused
            (before any side effect) when it has no inbox TODO-NNNN.md, is
            archived in done/, or is closed as done (DONE closure).

            Example (mid-run replan of the blocked unit TODO-0204)::

                dispatch_todo(project, "# TODO — refined task\\n...",
                              replan_for="TODO-0204")

            A hand-written ``replan_for: TODO-0204`` line in the content's
            contract block works the same way (the parameter then wins).
        claim_from: R2.2 (roadmap R2) — the id of a REJECTED/INTERRUPTED unit
            whose CODE this retry deliberately owns. The applied claim record
            (``.agentic/context/CLAIM-<source>.json``, written by
            ``claim_changes``) is read; its paths are excluded from the new
            baseline's untracked snapshot exactly like ``include_untracked``.
            This is the code channel — the R5 finding channel stays
            ``carry_over_from`` / ``include_untracked``. Refused (before any
            side effect) when no claim record exists or a claimed change is no
            longer in the tree.

    The dirty-source-tree policy (R2.2) governs the dispatch: a project with
    ``config_version`` and ``dirty_policy=require_clean`` (the version
    default) refuses a dirty source tree BEFORE reserving the id — no TODO,
    no baseline. ``dirty_policy=isolate`` proceeds and reports the excluded
    operations plus a dependency warning. A legacy project (no
    ``config_version``) keeps the pre-R2.2 ``warn`` behaviour and the result
    carries the migration warning. A dirty path covered by an ADOPTED channel
    — ``claim_from`` (code) or ``carry_over_from`` / ``include_untracked``
    (finding) — is owned, not refused.

    Re-dispatch of a number with stale BLOCKED/ACK closures (RUN3 #4)
    clears them automatically; a DONE closure refuses the dispatch
    (use ``awf restore`` for an archived TODO).

    Returns:
        DispatchTodoResult with todo_id, baseline_sha, files written,
        ``renumbered`` (REPORTS26 F5: True when the auto-issued
        number replaced a foreign one in the first line's heading),
        ``verify_path_warnings`` (TODO-0186: the contract's verify: path
        tokens missing from the project — a warning, never a refusal) and
        ``dirty_files``/``dirty_warning`` (TODO-0195: the tracked-dirty /
        staged / untracked paths present at dispatch — they will NOT enter
        the unit's commit; a warning, never a refusal). R2.2 adds
        ``dirty_policy`` (the effective policy), ``dirty_policy_warning``
        (legacy migration), ``isolate_warning``/``isolate_dependency_warning``
        (the isolate artefacts) and ``claim_from``/``claimed_files`` (the code
        transfer applied).

    Raises:
        AwfApiError: if .agentic/ missing, content empty, ``todo_id`` is
            already archived in done/, a DONE closure for ``todo_id`` is
            still in the outbox, a ``carry_over_from`` origin cannot be
            validated, an ``include_untracked`` path is invalid, an
            explicit ``todo_id`` disagrees with the first line's heading
            number without ``allow_mismatch`` (REPORTS26 F5),
            ``replan_for`` is not a TODO id / references a unit that does
            not exist or is closed as done (TODO-0213), the dirty-source-tree
            policy is ``require_clean`` and the tree is dirty (R2.2), or
            ``claim_from`` has no applied claim / its confirmed plan is
            invalidated by a later tree change (R2.2).
    """
    if not content or not content.strip():
        raise AwfApiError("content is required (non-empty TODO body)")

    # RUN3 #2 (Part B): an explicit pipeline= wins over a copy-pasted block
    # — inject first, so the parse below validates the FINAL content.
    if pipeline is not None and str(pipeline).strip():
        from ..unit_contract import inject_pipeline_key

        try:
            content = inject_pipeline_key(content, str(pipeline))
        except ValueError as e:
            raise AwfApiError(f"pipeline: {e}") from None

    # TODO-0213 (BD-30): same pattern — the explicit replan_for= writes the
    # marker into the contract block (created when absent) before the parse
    # below validates the FINAL content. check_replan_for inside
    # inject_replan_for refuses a value that is not a TODO-NNNN id.
    if replan_for is not None and str(replan_for).strip():
        from ..unit_contract import inject_replan_for

        try:
            content = inject_replan_for(content, str(replan_for))
        except ValueError as e:
            raise AwfApiError(f"replan_for: {e}") from None

    # U3: optional unit-contract block at the top of the TODO (--- yaml ---)
    # is validated here, before anything is written. No block = no-op.
    from ..unit_contract import parse_todo_contract
    try:
        _contract, unknown_keys = parse_todo_contract(content)
    except ValueError as e:
        raise AwfApiError(f"TODO contract block: {e}") from None

    # REPORTS26 B2: write entry point — a bare .agentic/ without
    # config.yaml (stale directory) is refused before any side effect.
    # The resolved path lands in the result (resolved_project_dir).
    project_dir = require_awf_project(project_dir)

    # R2.2 (roadmap R2): the explicit dirty-source-tree policy. Resolved from
    # config.yaml; a legacy project (no config_version) keeps the pre-R2.2
    # 'warn' behaviour and announces the migration. Read BEFORE any side
    # effect: require_clean refuses a dirty tree before the id is reserved —
    # nothing is written (no TODO, no baseline).
    from .. import config as _cfg_mod

    policy_decision = resolve_dirty_policy(_cfg_mod.load(project_dir))
    source_report = check_source_tree(project_dir)

    # R2.2 claim_changes: the applied CODE transfer this unit owns. Validated
    # before any side effect — a missing record, or a claimed change whose blob
    # moved since the plan (F3: consent is bound to the EXACT plan), refuses.
    # The validation lives in ``claim_changes`` (its own channel), the parallel
    # of ``resolve_carry_over`` / ``resolve_include_untracked`` below.
    claimed: list[str] = []
    if claim_from is not None and str(claim_from).strip():
        from ..claim_changes import resolve_claim_changes

        claimed = resolve_claim_changes(project_dir, str(claim_from))

    # RUN5 #1 (leak-gate): validate the carry-over BEFORE the policy gate and
    # the id reservation — a refused carry-over must leave no side effects, and
    # its paths are the FINDING channel's adopted dirt (F2).
    carry_over: set[str] = set()
    carry_over_files: list[str] = []
    if carry_over_from:
        from ..reject_files import resolve_carry_over

        carry_over_files = resolve_carry_over(project_dir, carry_over_from)
        carry_over = set(carry_over_files)

    # RUN10 #4 (TODO-0074): validate include_untracked BEFORE the policy gate
    # and the id reservation — a refused path must leave no side effects, and
    # its paths are the FINDING channel's adopted dirt (F1): a tree whose ONLY
    # dirt is an explicitly adopted untracked file must not be refused.
    include_files: list[str] = []
    if include_untracked is not None:
        from ..include_untracked import resolve_include_untracked

        include_files = resolve_include_untracked(project_dir, include_untracked)

    # R2.2: the adopted (owned) paths — the CODE transfer (claim_from) plus the
    # R5 FINDING transfer (carry_over_from / include_untracked). They are the
    # unit's own work, not un-owned dirt: excluded from the require_clean
    # refusal, from the dirty warning below AND from the isolate table (the
    # three surfaces describe one thing and must agree).
    adopted: set[str] = set(claimed) | carry_over | set(include_files)

    # The UN-OWNED part of the source tree — dirt no transfer channel adopted.
    # The SAME report feeds the require_clean refusal and the isolate table:
    # an adopted path is the unit's work, so it is neither refused nor listed
    # as "stay OUTSIDE the unit".
    unowned_report = SourceTreeReport(
        paths=tuple(p for p in source_report.paths if p not in adopted),
        managed=source_report.managed,
        error=source_report.error,
    )

    if policy_decision.policy == REQUIRE_CLEAN and not unowned_report.clean:
        raise AwfApiError(format_dirty_refusal(unowned_report))

    # TODO-0213 (BD-30): the marker must reference a real, live unit — the
    # escalated (blocked) TODO the replan replaces. A unit that does not
    # exist, is archived, or is closed as done could never be picked up
    # (there is no escalation waiting for it) — refuse BEFORE any side
    # effect (no TODO, no baseline).
    if replan_for is not None and str(replan_for).strip():
        target = str(replan_for).strip()
        if (paths.done_dir(project_dir) / target).is_dir():
            raise AwfApiError(
                f"replan_for {target}: the unit is already archived in "
                f"done/{target}/ — a replan replaces a unit that is still "
                "waiting (blocked), not a finished one"
            )
        if not (paths.inbox(project_dir) / f"{target}.md").is_file():
            raise AwfApiError(
                f"replan_for {target}: no such TODO — the marker must "
                "reference the unit being replaced (its inbox TODO-NNNN.md)"
            )
        from .hygiene import has_done_closure

        if has_done_closure(project_dir, target):
            raise AwfApiError(
                f"replan_for {target}: the unit is closed as done (DONE "
                "closure in outbox) — a replan replaces a blocked unit, "
                "not a finished one"
            )

    # TODO-0186: verify-path validation at dispatch. A verify: command may
    # reference a file that does not exist (a typo, a renamed test) — that
    # would surface later as a red contract gate. Extract the path tokens
    # now and collect the missing ones as a WARNING (never a refusal: the
    # heuristic has no right to block a dispatch).
    verify_path_warnings: list[str] = []
    if _contract is not None:
        verify_cmds = _contract.get("verify")
        if isinstance(verify_cmds, list):
            verify_path_warnings = _missing_verify_paths(
                project_dir, _verify_path_tokens(verify_cmds)
            )

    # RUN5 #1 (leak-gate) / RUN10 #4 (TODO-0074): the carry-over and the
    # include_untracked lists were validated ABOVE (before the policy gate),
    # since their paths are the adopted dirt the gate must not refuse. They are
    # re-used here to build the baseline exclusions.

    # Whether the CALLER fixed the id (explicit collisions must raise,
    # auto-picked ones may retry with the next free id).
    explicit_id = todo_id is not None
    if todo_id is None:
        todo_id = _next_todo_id(project_dir)
    elif not is_valid_todo_id(todo_id):
        raise AwfApiError(
            f"invalid todo_id '{todo_id}' — expected format 'TODO-NNNN' (4+ digits)"
        )

    # REPORTS26 F5: an explicit id + a foreign number in the first line's
    # heading would land a unit titled after someone else's number. Refuse
    # BEFORE any side effect (no reservation, no baseline); allow_mismatch
    # is the conscious opt-out (content written as-is).
    heading_num = _heading_todo_number(content)
    if (
        explicit_id
        and not allow_mismatch
        and heading_num is not None
        and heading_num != int(todo_id.split("-", 1)[1])
    ):
        raise AwfApiError(
            f"the content heading says TODO-{heading_num:04d} but "
            f"todo_id is {todo_id} — the unit would land with a foreign "
            "number in its title. Fix the heading to match, or pass "
            "allow_mismatch=True to write the content as-is."
        )

    # A-12 (audit 2026-09-25): an archived id is occupied — a re-dispatch
    # of done/<id>/ would let the next archive overwrite the history.
    # Refuse BEFORE the O_EXCL reservation: no file created, no baseline.
    # Auto numbering already skips done/<id>/ (see _next_todo_id), so this
    # check bites on explicit todo_id.
    if (paths.done_dir(project_dir) / todo_id).is_dir():
        raise AwfApiError(
            f"{todo_id} is already archived in done/{todo_id}/ — the id is "
            "occupied. Restore it with awf restore or use a different todo_id."
        )

    inbox = paths.inbox(project_dir)
    # R1.5 (добор 0215): the inbox lives under ``.agentic`` — a symlinked
    # ``.agentic``/``inbox`` used to send the unit file and its ``.ready``
    # signal OUTSIDE the project (S9-01 class). Refuse before the first side
    # effect; the writes below go through the no-follow managed helpers too.
    try:
        managed_fs.assert_no_symlink_escape(inbox, root=project_dir)
    except managed_fs.ManagedPathError as e:
        raise AwfApiError(f"dispatch refused — unsafe inbox path: {e}") from e
    # F-0219-2: no ``inbox.mkdir`` here. A plain ``Path.mkdir(parents=True)``
    # after the preflight re-follows ``.agentic``: a swap to an outward
    # symlink between the check and the mkdir created an empty ``inbox``
    # OUTSIDE the project (F-B class, same as F-0219-1 for logs). The chain
    # is created safely below by ``open_managed(create=True)`` — ``mkdirat``
    # on the already-verified parent fd, which refuses a swapped component
    # (ELOOP → ManagedPathError) before any directory appears.

    # AUD14-01: reserve the ID atomically before writing content. The old
    # exists() → write() window let two parallel dispatches pick the same
    # TODO-NNNN and the second silently overwrote the first's content.
    # O_CREAT|O_EXCL makes the claim one syscall: the first writer wins,
    # concurrent callers get FileExistsError and retry with the next id.
    md_path: Path | None = None
    for _attempt in range(16):
        candidate = inbox / f"{todo_id}.md"
        try:
            # R1.5: no-follow all along — a swapped intermediate component
            # fails closed (ManagedPathError), never a write outside.
            fd = managed_fs.open_managed(
                candidate,
                flags=os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                mode=0o644,
                root=project_dir,
            )
        except FileExistsError:
            if explicit_id:
                raise AwfApiError(
                    f"{candidate.name} already exists in inbox. Use a different "
                    "todo_id or remove the existing file first."
                ) from None
            todo_id = _next_todo_id(project_dir)
            # desynchronize the herd of parallel callers
            import random as _random
            import time as _time
            _time.sleep(_random.uniform(0, 0.02))
            continue
        except managed_fs.ManagedPathError as e:
            raise AwfApiError(f"dispatch refused — unsafe inbox path: {e}") from e
        os.close(fd)
        md_path = candidate
        break
    if md_path is None:
        raise AwfApiError(
            f"could not reserve a free TODO id (16 consecutive collisions, "
            f"last tried {todo_id}) — check .agentic/inbox for stray TODO files"
        )

    # REPORTS26 F5: auto number + a foreign number in the first line's
    # heading (a copied template kept its old title) — renumber the
    # heading to the ISSUED id (the reservation loop may have moved past
    # the first candidate on collisions, hence here, not earlier). The
    # rest of the content stays untouched; an explicit id never reaches
    # here (mismatch refused above, matching number is a no-op).
    renumbered = False
    if not explicit_id and heading_num is not None:
        if heading_num != int(todo_id.split("-", 1)[1]):
            content = _renumber_heading(content, todo_id)
            renumbered = True

    # Optional role hint as HTML comment (invisible to LLM reading the .md,
    # visible to grep / debugging).
    body = content
    if role:
        body = f"<!-- role_hint: {role} -->\n" + body

    # RUN3 #4: never (re)issue a number whose DONE closure is still in the
    # outbox — the fresh TODO would be invisible (todos.is_closed). The
    # archived TODO comes back via awf restore, not a silent re-dispatch.
    from .hygiene import clear_stale_closures, has_done_closure

    if has_done_closure(project_dir, todo_id):
        md_path.unlink(missing_ok=True)
        raise AwfApiError(
            f"outbox has a DONE closure for {todo_id} — the TODO is closed as "
            "finished. Bring it back with awf restore or dispatch a new number."
        )

    # Pre-dispatch check: grep code for key identifiers from TODO content.
    # Warns if patterns already exist in codebase (task may be already done).
    pre_check_warnings: list[str] = []
    try:
        import re as _re
        # Extract identifiers: backtick-quoted, camelCase, snake_case
        identifiers = set()
        for m in _re.finditer(r'`([a-zA-Z_][a-zA-Z0-9_]{2,})`', content):
            identifiers.add(m.group(1))
        for m in _re.finditer(r'\b([a-z][a-zA-Z0-9]*_[a-z][a-zA-Z0-9_]*)\b', content):
            identifiers.add(m.group(1))
        identifiers.discard("todo")  # too generic

        if identifiers:
            import subprocess as _sp
            for ident in list(identifiers)[:5]:  # check max 5
                try:
                    result = _sp.run(
                        ["grep", "-rl", "--include=*.ts", "--include=*.js",
                         "--include=*.py", "--include=*.go", "--include=*.rs",
                         "--include=*.java", "--include=*.rb",
                         "-d", "skip",
                         # AUD15-05: don't scan VCS/dependency dirs — on a
                         # 25k-file tree the old unfiltered grep took up to
                         # 5s per identifier (5 identifiers → ~25s dispatch).
                         "--exclude-dir=.git", "--exclude-dir=node_modules",
                         "--exclude-dir=.venv", "--exclude-dir=venv",
                         "--exclude-dir=vendor",
                         ident, str(project_dir)],
                        capture_output=True, text=True, timeout=2, check=False,
                    )
                    # Filter out .agentic/ matches
                    matches = [line for line in result.stdout.strip().splitlines()
                               if ".agentic/" not in line and "node_modules/" not in line]
                    if matches:
                        pre_check_warnings.append(
                            f"'{ident}' already found in {len(matches)} file(s). "
                            f"Task may be already implemented — verify before running pipeline."
                        )
                except (OSError, _sp.SubprocessError):
                    pass
    except Exception:
        pass  # pre-check is best-effort, never blocks dispatch

    # U3: unknown keys in the contract block are a typo risk — warn, don't fail.
    from ..unit_contract import _CONTRACT_KEYS

    for key in unknown_keys:
        pre_check_warnings.append(
            f"contract block: unknown key '{key}' — known keys: "
            + ", ".join(_CONTRACT_KEYS)
        )

    # TODO-0186: surface the missing verify paths as one warning line (the
    # structured field is verify_path_warnings on the result). The dispatch
    # still succeeds — a false positive would be worse than a missed typo.
    if verify_path_warnings:
        pre_check_warnings.append(
            "verify: command(s) reference path(s) not found in the project — "
            + ", ".join(verify_path_warnings)
            + " (warning only; the contract gate may go red)"
        )

    # Step 1: fill the reserved TODO-NNNN.md (atomic temp+rename over the
    # placeholder — the id is already claimed, no one else can take it).
    # R1.5: no-follow write (same class as the reservation open above).
    managed_fs.managed_write_text(md_path, body, root=project_dir, mode=0o644)

    # Step 2: create baseline snapshot (sha + tests.log + env.log + status).
    # RUN10 #4: include files are excluded from the untracked snapshot by a
    # SEPARATE parameter — the leak-gate carry-over stays its own mechanism.
    try:
        baseline = create_baseline(
            project_dir, todo_id, carry_over=carry_over,
            include=set(include_files) or None,
            claim=set(claimed) or None,
        )
    except Exception:
        # Rollback: remove TODO .md if baseline fails (prevents orphan TODO)
        md_path.unlink(missing_ok=True)
        raise

    # RUN5 #1 (leak-gate): audit trail — which rejected origin this retry's
    # baseline carried over. Best-effort: a failed write must not undo the
    # dispatch (the baseline already excluded the paths). The link lifecycle is
    # owned by ``reject_files`` (the leak-gate channel), the same as the
    # include/claim links, instead of being re-spelled inline here.
    if carry_over:
        from ..reject_files import write_carry_over_link

        write_carry_over_link(project_dir, todo_id, str(carry_over_from))
        from .._log import log as _log

        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — carried over {len(carry_over_files)} "
            f"untracked file(s) from rejected {carry_over_from} (leak-gate)",
        )
    else:
        # RUN5 #1 (leak-gate): a non-carry-over (re-)dispatch rewrites the
        # baseline WITHOUT the exclusion — drop any stale link from an earlier
        # carry-over dispatch of the same id, so a later re-baseline (awf
        # run_next) does not re-apply an exclusion that no longer holds.
        from ..reject_files import clear_carry_over_link

        clear_carry_over_link(project_dir, todo_id)

    # RUN10 #4 (TODO-0074): audit trail — which pre-existing untracked files
    # this unit re-claims; a (re-)dispatch without include_untracked drops
    # the stale link (same lifecycle as the carry-over link above).
    from ..include_untracked import clear_include_audit, write_include_audit

    if include_files:
        write_include_audit(project_dir, todo_id, include_files)
    else:
        clear_include_audit(project_dir, todo_id)

    # R2.2 (TODO-0221 M2): CODE-channel audit link — the claim parallel to the
    # carry_over/include links above. Without it a later re-baseline
    # (awf run_next) drops the claimed paths' exclusion and the commit gate
    # treats a claimed predecessor blob as pre-existing (silently out of the
    # unit commit). A (re-)dispatch without claim_from drops a stale link.
    from ..claim_changes import clear_claim_link, write_claim_link

    if claimed and claim_from is not None and str(claim_from).strip():
        write_claim_link(project_dir, todo_id, str(claim_from).strip())
    else:
        clear_claim_link(project_dir, todo_id)

    # RUN3 #4: re-dispatch of the same number must not stay hidden behind
    # stale BLOCKED/ACK closures — clear them via the shared helper.
    # (Only reachable when inbox had no .md for this id, i.e. a re-issue;
    # a running pipeline keeps its own .md in the inbox, so its signals
    # are never moved out from under the engine.)
    cleared, _trace = clear_stale_closures(project_dir, todo_id)
    if cleared:
        from .._log import log as _log

        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — stale closure cleared on re-dispatch: "
            f"{', '.join(cleared)}",
        )

    # Step 3: dispatch signal
    # R1.5: the `.ready` touch is a write under `.agentic` too — no-follow.
    ready_path = inbox / f"{todo_id}.ready"
    try:
        managed_fs.managed_write_text(ready_path, "", root=project_dir, mode=0o644)
    except managed_fs.ManagedPathError as e:
        raise AwfApiError(f"dispatch refused — unsafe inbox path: {e}") from e

    # RUN10 #4 (TODO-0074): visibility — the files the commit gate will
    # exclude (still untracked, recorded in the fresh baseline snapshot,
    # NOT re-claimed via carry_over/include_untracked).
    from ..include_untracked import format_untracked_warning, pre_existing_untracked

    pre_existing = pre_existing_untracked(project_dir, todo_id)
    untracked_warning = format_untracked_warning(todo_id, pre_existing)

    # TODO-0195 (рана C) / TODO-0204 (REPORTS33): a dirty tree at dispatch
    # (tracked-dirty / staged / untracked, gitignored excluded) is LOUD, not
    # a refusal. Same moment as the baseline's own .status snapshot, so the
    # warning describes the tree the baseline actually saw — and it must
    # promise what the commit gate WILL do: with the dispatch-time worktree
    # snapshot the pre-dispatch change of a tracked-dirty file stays OUTSIDE
    # the unit commit (only the unit's own edit lands); without it the
    # warning says the change will enter the commit as-is.
    # One R2.1 model read serves both the warning's file list and its class
    # map (they describe the same tree): ``dirty_files`` is the keys of
    # ``dirty_classes`` in the model's first-seen order.
    dirty_classes = {
        p: c for p, c in _dirty_tree_classes(project_dir).items() if p not in adopted
    }
    dirty_files = list(dirty_classes)
    snapshot_available = (
        paths.context_dir(project_dir) / f"BASELINE-{todo_id}.worktree"
    ).is_file()
    snapshot_reason = ""
    if not snapshot_available and dirty_classes:
        snapshot_reason = (
            "not a git repo"
            if not baseline.is_git_repo
            else "the worktree snapshot was not taken"
        )
    dirty_warning = _format_dirty_warning(
        todo_id, dirty_files, dirty_classes, snapshot_available, snapshot_reason
    )

    # R2.2 (roadmap R2): the isolate artefacts — what is excluded and what may
    # depend on it. Only the explicit isolate policy reports them (legacy
    # 'warn' keeps the plain dirty warning above).
    isolate_warning = ""
    isolate_dependency_warning = ""
    if policy_decision.policy == ISOLATE:
        isolate_warning, isolate_dependency_warning = format_isolate_report(
            todo_id, unowned_report
        )
    log_policy_decision(project_dir, todo_id, policy_decision)

    # F3: the path lists in the RESULT are a display surface too — the MCP
    # answer serializes them (json.dumps ... ensure_ascii=False). Escape each
    # name for the UI so a non-UTF-8 byte cannot break that sink. The internal
    # lists (adopted-filtered ``dirty_files``, ``claimed``) stay on the raw
    # display form the ownership decisions above used.
    from ..infra.git_model import escape_str_for_ui

    return DispatchTodoResult(
        todo_id=todo_id,
        baseline_sha=baseline.sha,
        resolved_project_dir=str(project_dir),
        role_hint=role,
        files_written=[
            f".agentic/inbox/{todo_id}.md",
            f".agentic/inbox/{todo_id}.ready",
            *baseline.files_created,
        ],
        pre_check_warnings=pre_check_warnings,
        carry_over_from=carry_over_from,
        carry_over_files=[escape_str_for_ui(p) for p in carry_over_files],
        pre_existing_untracked=[escape_str_for_ui(p) for p in pre_existing],
        untracked_warning=untracked_warning,
        renumbered=renumbered,
        verify_path_warnings=verify_path_warnings,
        dirty_files=[escape_str_for_ui(p) for p in dirty_files],
        dirty_warning=dirty_warning,
        dirty_policy=policy_decision.policy,
        dirty_policy_warning=policy_decision.warning,
        isolate_warning=isolate_warning,
        isolate_dependency_warning=isolate_dependency_warning,
        claim_from=(str(claim_from).strip() if claim_from else None),
        claimed_files=[escape_str_for_ui(p) for p in claimed],
    )


__all__ = ["dispatch_todo", "_next_todo_id"]
