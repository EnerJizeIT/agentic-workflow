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
synced for the plan's files (``_sync_real_index``) — otherwise it stays
on the pre-commit base and the next unit's plan subtracts the unit's own
committed files as "foreign" (A-17).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import commit_plan, git_utils, paths
from ._atomic import atomic_write_text
from ._log import log as _log

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


def _sync_real_index(project_dir: Path, plan: commit_plan.CommitPlan) -> str:
    """R-03-F1 (TODO-0093): re-point the user's index at the new HEAD for
    the plan's files.

    The commit ran on a throwaway ``GIT_INDEX_FILE`` — the user's index
    still holds the pre-commit base for every plan file. Stale, it makes
    the plan's files look "staged vs the new HEAD" (``git diff
    --cached``): the next unit (baseline = the new HEAD) subtracts its own
    committed files from the plan as "foreign" (incident A-17:
    ``awf/metrics.py`` silently dropped from a unit commit), and
    ``git status`` shows committed work as staged.

    ``git reset -- <paths>`` is the pinpoint form: it re-points ONLY the
    listed paths' index entries at the new HEAD — the working tree is not
    touched and foreign staged entries outside the plan keep their state
    (A-01).

    Returns "" on success, the warning text on failure. The commit is
    already made — a failed sync degrades to the old stale-index behavior
    and must stay visible, not roll back a good commit.
    """
    if not plan.files:
        return ""
    try:
        result = subprocess.run(
            ["git", "reset", "-q", "HEAD", "--", *plan.files],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11: a hung git must not hang the commit gate
        )
    except (subprocess.SubprocessError, OSError) as e:
        return f"index sync after the unit commit failed: {e}"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return (
            f"index sync after the unit commit failed "
            f"(rc={result.returncode}): {detail}"
        )
    return ""


def _short_paths(paths_list: list[str], cap: int = 10) -> str:
    """The first ``cap`` paths for a refusal/audit message (… when more)."""
    shown = paths_list[:cap]
    more = len(paths_list) - len(shown)
    return ", ".join(shown) + (f" …({more} more)" if more else "")


# ─── REPORTS33 (TODO-0204): the unit delta of a pre-dirty file ─────────


def _parse_delta_hunks(
    patch_text: str,
) -> list[tuple[int, list[tuple[str, str]]]] | None:
    """The ``@`` hunks of a unified diff as ``(base_pos, ops)`` pairs, or
    None when the text is not a parseable text diff (binary, garbage).

    ``base_pos`` — the 0-based position in the BASE content the hunk is
    anchored at; ``ops`` — ``(op, text)`` lines with op in ``{' ', '-',
    '+'}`` (a blank context line arrives as ``("", "")``-normalized to
    ``(" ", "")``).
    """
    hunks: list[tuple[int, list[tuple[str, str]]]] = []
    in_hunk = False
    for line in patch_text.splitlines():
        if line.startswith("@@"):
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@", line)
            if m is None:
                return None
            in_hunk = True
            hunks.append((int(m.group(1)) - 1, []))
        elif in_hunk:
            if line.startswith(("diff --git", "Binary files")):
                in_hunk = False  # a next-file header (a pathspec keeps one)
            elif line == "":
                hunks[-1][1].append((" ", ""))  # a blank context line
            elif line[:1] in (" ", "-", "+"):
                hunks[-1][1].append((line[:1], line[1:]))
            elif line.startswith("\\"):
                continue  # "\ No newline at end of file"
            else:
                return None  # an unexpected line — not a text diff
    if not hunks or not any(hunk for _, hunk in hunks):
        return None
    return hunks


def _head_position(
    snap_line: int,
    pre_hunks: list[tuple[int, list[tuple[str, str]]]],
) -> int:
    """The 0-based position in HEAD of the snapshot line ``snap_line``
    (QA F1, REVIEW 2026-10-12: the exact hunk anchor in HEAD coordinates).

    ``pre_hunks`` — the parsed snapshot→HEAD diff (``_parse_delta_hunks``):
    each hunk is ``(base_pos in snapshot coordinates, ops)`` with op in
    ``{' ', '-', '+'}``. Insertions ('+') before the line raise its
    position in HEAD, deletions ('-') before it lower it. A pre-dispatch
    hunk that removes the line itself still yields a position (the line
    is simply absent from HEAD — the caller's verbatim match fails and
    degrades); a hunk that merely surrounds the line is exact.
    """
    shift = 0
    for base_pos, ops in pre_hunks:
        if base_pos > snap_line:
            continue  # the hunk starts after the line — no effect
        pos = base_pos
        delta = 0
        found = False
        for op, _text in ops:
            if op == "+":
                delta += 1
            elif op == "-":
                delta -= 1
            if op != "+":
                if pos == snap_line:
                    shift += delta
                    found = True
                    break
                pos += 1
        if not found:
            # The line was deleted by this hunk (a '-' line), or the hunk
            # ends before it: the full hunk delta applies either way.
            shift += delta
    return snap_line + shift


def _apply_unit_delta(
    head_text: str,
    patch_text: str,
    pre_hunks: list[tuple[int, list[tuple[str, str]]]],
) -> str | None:
    """Apply the unit's edit (the snapshot→worktree diff) to the HEAD
    content — the content the unit commit must carry.

    A hunk's context may name pre-dispatch lines that are ABSENT from
    HEAD: with a fuzz of up to 2 the hunk may fail to match only at its
    EDGE context (deleted lines and the inner context must match
    verbatim). The anchor is computed exactly in HEAD coordinates
    (``_head_position``: the snapshot position shifted by the
    pre-dispatch insertions/deletions before it) and only the EXACT
    anchor is accepted: a verbatim match at ``anchor`` places the hunk,
    a miss tries the next fuzz variant, and when no variant places it
    the merge returns None (QA F1: an unbounded from-0 scan placed the
    unit's edit into the first duplicated context far away; QA F3: a
    window scan from ``anchor - window`` let a duplicate pattern inside
    the window capture it; REVIEW iter 4: the window scan was removed
    outright — it was unreachable for placeable hunks, whose anchors
    are exact, and could only fire on an unplaceable hunk, where
    matching anywhere is an F1-class corruption). A hunk whose base
    side is all context (an empty seq after the fuzz drop) is never
    placed either: its old placement at ``min(base_pos + shift,
    len(target))`` was prepend semantics, ignoring both "the addition
    comes AFTER the context" and the pre-dispatch shift — the unit's
    lines could land in the middle (or the start) of the file silently
    (QA F5); a placeable hunk matches a non-empty variant at its exact
    anchor, otherwise the merge degrades. A hunk with no base side at
    all (``base_pos < 0``: the file was emptied before the dispatch,
    git ``@@ -0,0``) has no anchor in HEAD — the merge would splice at
    a negative index and leak HEAD lines into the commit; it is
    unplaceable and returns None (QA F4). CRLF content is never merged
    either: ``splitlines()`` eats the ``\\r`` (in the HEAD line split and
    the patch parse alike) and the join reassembles the file with LF —
    a ``\\r`` in ``head_text`` or ``patch_text`` means the merge would
    normalize the whole file's line endings silently (the worktree keeps
    CRLF — phantom dirt after the commit), so it returns None (QA F7).
    Returns the merged content, or None when a hunk cannot be placed
    (overlap, or no anchor) or the content is CRLF — the caller
    degrades to the legacy full-worktree commit, loudly.
    """
    if "\r" in head_text or "\r" in patch_text:
        # QA F7 (REVIEW 2026-10-12, iter 6): CRLF — see the docstring.
        return None
    hunks = _parse_delta_hunks(patch_text)
    if hunks is None:
        return None
    target = head_text.splitlines()
    shift = 0
    for base_pos, hunk in hunks:
        expected = [(op, text) for op, text in hunk if op != "+"]
        lead_ctx = 0
        while lead_ctx < len(expected) and expected[lead_ctx][0] == " ":
            lead_ctx += 1
        trail_ctx = 0
        while (
            trail_ctx < len(expected)
            and expected[len(expected) - 1 - trail_ctx][0] == " "
        ):
            trail_ctx += 1
        placed: tuple[int, int, list[str]] | None = None
        for lead in range(0, min(2, lead_ctx) + 1):
            for trail in range(0, min(2, trail_ctx) + 1):
                if lead + trail > 2:
                    continue
                end = len(expected) - trail if trail else len(expected)
                seq = [text for _, text in expected[lead:end]]
                if not seq:
                    # QA F5 (REVIEW 2026-10-12): the fuzz dropped the
                    # whole base side — the variant has no anchor of its
                    # own. The old placement at min(base_pos + shift,
                    # len(target)) was prepend semantics: it ignored
                    # both "the addition comes AFTER the context" and
                    # the pre-dispatch shift, and could splice the
                    # unit's lines into the middle (or the start) of
                    # the file silently. Never place it: a placeable
                    # hunk matches a non-empty variant at its exact
                    # anchor, otherwise the merge degrades loudly below.
                    continue
                anchor = _head_position(base_pos + lead, pre_hunks) + shift
                if anchor < 0 or target[anchor:anchor + len(seq)] != seq:
                    # the exact anchor missed: the next fuzz variant,
                    # never a search elsewhere — matching away from the
                    # anchor means a duplicated context pattern
                    # captured the edit (F1/F3 class).
                    continue
                out: list[str] = []
                e = 0
                for op, text in hunk:
                    if op == "+":
                        out.append(text)
                    else:
                        if op == " " and lead <= e < end:
                            out.append(text)
                        e += 1
                placed = (anchor, len(seq), out)
                break
            if placed is not None:
                break
        if placed is None:
            return None
        idx, span, out = placed
        target = target[:idx] + out + target[idx + span:]
        shift += len(out) - span
    text = "\n".join(target)
    if target and (not head_text or head_text.endswith("\n")):
        text += "\n"
    return text


def _commit_delta_file(
    project_dir: Path,
    env: dict[str, str],
    file: str,
    snap_sha: str,
    logs_dir: Path | None,
) -> None:
    """Commit ONLY the unit's own edit of a tracked-dirty-at-baseline file
    (REPORTS33, TODO-0204) into the throwaway index.

    The unit started from the dispatch-time worktree (the snapshot's
    blob) and produced the current worktree; the commit must carry that
    edit applied to the HEAD content, not the pre-dispatch WIP (which
    stays in the worktree uncommitted). The unit's edit is the
    snapshot→worktree diff applied to the HEAD content with the
    edge-context fuzz (the pre-dispatch lines are absent from HEAD); on a
    placeable failure (overlap, binary, CRLF line endings, any git
    error) the file degrades to the legacy full-worktree commit —
    loudly logged, never silent.
    """
    def _warn(msg: str) -> None:
        print(f"WARNING: {msg}", file=sys.stderr)
        if logs_dir is not None:
            _log(logs_dir, msg)

    def _degrade(why: str) -> None:
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: {why} — the full "
            "worktree content enters the unit commit (legacy behavior)"
        )
        add = _git_isolated(project_dir, ["add", "--", file], env)
        if add.returncode != 0:
            _warn(
                f"REPORTS33 delta fallback add for {file!r} failed "
                f"(rc={add.returncode}): "
                f"{add.stderr.strip() or add.stdout.strip()}"
            )

    # the three content states: HEAD / snapshot (pre-dispatch) / worktree
    head_r = _git_isolated(
        project_dir, ["rev-parse", "-q", "--verify", f"HEAD:{file}"], env
    )
    if head_r.returncode != 0:
        _degrade("the file is not in HEAD — the content merge is unavailable")
        return
    head_blob = head_r.stdout.strip()
    snap_r = _git_isolated(
        project_dir, ["rev-parse", "-q", "--verify", f"{snap_sha}:{file}"], env
    )
    snap_blob = snap_r.stdout.strip() if snap_r.returncode == 0 else None
    if (project_dir / file).is_file():
        unit_r = _git_isolated(project_dir, ["hash-object", "--", file], env)
        unit_blob = unit_r.stdout.strip() if unit_r.returncode == 0 else None
    else:
        unit_blob = None

    if unit_blob is None:
        # The file is deleted in the worktree: a pre-dispatch deletion
        # cannot "stay in the worktree" — commit the worktree state as-is.
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: the file is missing "
            "in the worktree (a deletion) — the worktree state is "
            "committed as-is"
        )
        _git_isolated(project_dir, ["add", "--", file], env)
        return
    if snap_blob is None:
        # Absent from the dispatch-time snapshot (deleted pre-dispatch);
        # what the unit (re)created is wholly its own edit.
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: the file is absent "
            "from the dispatch-time snapshot (deleted pre-dispatch) — the "
            "whole worktree content is committed as the unit's edit"
        )
        _git_isolated(project_dir, ["add", "--", file], env)
        return
    if unit_blob == snap_blob:
        # The worktree matches the snapshot — the unit never touched the
        # file since the dispatch (the plan would have excluded it; a
        # worktree move between the plan and the commit). No delta: keep
        # the HEAD index entry, the file is not in the commit.
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: the worktree matches "
            "the dispatch-time snapshot (no unit delta) — the file is "
            "left out of the unit commit"
        )
        return

    head_bytes = subprocess.run(
        ["git", "cat-file", "blob", head_blob],
        cwd=project_dir,
        capture_output=True,
        check=False,
        timeout=30,  # AUD04-11
        env=env,
    )
    if head_bytes.returncode != 0:
        _degrade("the HEAD content could not be read")
        return
    try:
        head_text = head_bytes.stdout.decode("utf-8")
    except UnicodeDecodeError:
        _degrade("binary content — the text merge is unavailable")
        return
    try:
        diff_r = _git_isolated(
            project_dir,
            ["diff", "--no-renames", "--no-color", snap_sha, "--", file],
            env,
        )
    except UnicodeDecodeError:
        _degrade("the snapshot→worktree diff is not text")
        return
    if diff_r.returncode != 0:
        _degrade(
            f"the snapshot→worktree diff failed (rc={diff_r.returncode})"
        )
        return
    # QA F1: the hunk anchors are computed in HEAD coordinates — the
    # snapshot→HEAD (pre-dispatch) diff tells how snapshot line positions
    # shift into HEAD. Without it the anchor is unknown: degrade loudly.
    try:
        pre_r = _git_isolated(
            project_dir,
            ["diff", "--no-renames", "--no-color", snap_sha, "HEAD", "--", file],
            env,
        )
    except UnicodeDecodeError:
        _degrade("the snapshot→HEAD diff is not text — the anchor is unavailable")
        return
    if pre_r.returncode != 0:
        _degrade(f"the snapshot→HEAD diff failed (rc={pre_r.returncode})")
        return
    pre_hunks = _parse_delta_hunks(pre_r.stdout)
    if pre_hunks is None:
        _degrade("the snapshot→HEAD diff is not a parseable text diff")
        return
    # QA F7 (REVIEW 2026-10-12, iter 6): CRLF in the HEAD content or the
    # unit diff — the text merge would normalize the line endings to LF
    # silently (splitlines() eats the \r, the join reassembles with LF).
    # Degrade loudly with the reason in the log; the legacy worktree
    # commit keeps CRLF intact.
    if "\r" in head_text or "\r" in diff_r.stdout:
        _degrade(
            "CRLF line endings in the HEAD content or the unit diff — "
            "the text merge would normalize them to LF (line endings) — "
            "no clean unit delta"
        )
        return
    merged = _apply_unit_delta(head_text, diff_r.stdout, pre_hunks)
    if merged is None:
        _degrade(
            "the unit edit cannot be merged into the HEAD content (it "
            "overlaps the pre-dispatch edit, or a hunk has no anchor — "
            "e.g. the file was emptied before the dispatch) — no clean "
            "unit delta"
        )
        return
    blob_r = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=project_dir,
        input=merged,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,  # AUD04-11
        env=env,
    )
    if blob_r.returncode != 0:
        _degrade("the merged content could not be written as a blob")
        return
    # QA F2 (REVIEW 2026-10-12): the mode is the file's HEAD mode, not a
    # hardcoded 100644 — an executable (100755) must not silently lose
    # its exec bit in the delta commit.
    mode_r = _git_isolated(
        project_dir, ["ls-tree", "HEAD", "--", file], env
    )
    if mode_r.returncode != 0:
        _degrade(
            f"the HEAD mode could not be read (rc={mode_r.returncode})"
        )
        return
    mode = mode_r.stdout.split()
    if not mode or mode[0] not in ("100644", "100755", "120000"):
        _degrade(
            f"unexpected HEAD mode {mode[0] if mode else '?'} — refusing "
            "to guess the file mode"
        )
        return
    upd = _git_isolated(
        project_dir,
        [
            "update-index",
            "--cacheinfo",
            f"{mode[0]},{blob_r.stdout.strip()},{file}",
        ],
        env,
    )
    if upd.returncode != 0:
        _degrade(
            "the merged content could not be seeded into the isolated index "
            f"(rc={upd.returncode}): {upd.stderr.strip() or upd.stdout.strip()}"
        )
        return


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
        result = subprocess.run(
            ["git", "log", "--format=%s", f"{baseline_sha}..HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11: hung git must not hang the commit gate
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
) -> commit_plan.CommitOutcome:
    """R-03: commit EXACTLY ``plan.files`` through a throwaway index.

    The isolated index is built from HEAD (when commits exist) plus the
    plan's files and deleted in ``finally``. On success the user's index
    is synced for the plan's files (R-03-F1: their entries re-pointed at
    the new HEAD, so the next unit's plan does not subtract its own
    committed files as "foreign"); foreign staged entries outside the
    plan are never touched. On refusal or error the index is
    byte-identical (nothing was ever staged into it).
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
            # REPORTS33 (TODO-0204): a tracked-dirty-at-baseline file is
            # committed as the unit's delta ONLY (the pre-dispatch WIP
            # stays in the worktree); everything else is added as before.
            delta = set(plan.delta_files)
            plain = [f for f in plan.files if f not in delta]
            if plain:
                add = _git_isolated(project_dir, ["add", "--", *plain], env)
                if add.returncode != 0:
                    return commit_plan.CommitOutcome(
                        commit_plan.OUTCOME_ERROR,
                        f"git add failed (rc={add.returncode}): "
                        f"{add.stderr.strip() or add.stdout.strip()}",
                    )
            if delta:
                snap_sha = commit_plan.read_worktree_snapshot(
                    project_dir, plan.todo_id
                )
                if not snap_sha:
                    # The plan marked delta files but the snapshot is gone
                    # (moved out from under the gate) — legacy behavior.
                    add = _git_isolated(
                        project_dir, ["add", "--", *sorted(delta)], env
                    )
                    if add.returncode != 0:
                        return commit_plan.CommitOutcome(
                            commit_plan.OUTCOME_ERROR,
                            f"git add failed (rc={add.returncode}): "
                            f"{add.stderr.strip() or add.stdout.strip()}",
                        )
                else:
                    for file in sorted(delta):
                        _commit_delta_file(
                            project_dir, env, file, snap_sha, logs_dir
                        )
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
        # R-03-F1: the throwaway index never touched the user's one — its
        # plan-file entries still hold the pre-commit base. Re-point them
        # at the new HEAD (pinpoint: only the plan's paths).
        sync_warning = _sync_real_index(project_dir, plan)
        if sync_warning:
            print(f"WARNING: {sync_warning}", file=sys.stderr)
            if logs_dir is not None:
                _log(logs_dir, f"R-03-F1: {sync_warning}")
        return commit_plan.CommitOutcome(
            commit_plan.OUTCOME_COMMITTED, "", sha.stdout.strip()
        )
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
    record = {
        "todo_id": plan.todo_id,
        "status": outcome.status,
        "excluded": [[p, r] for p, r in plan.excluded],
        "worker_staged": list(plan.worker_staged),
        "skip_inconsistent": skip_inconsistent,
        "skip_reason": outcome.reason if skip_inconsistent else "",
    }
    try:
        atomic_write_text(
            paths.context_dir(project_dir) / f"COMMIT-AUDIT-{plan.todo_id}.json",
            json.dumps(record, ensure_ascii=False, indent=1) + "\n",
        )
    except OSError as e:
        _log(logs_dir, f"TODO-0195: commit audit for {plan.todo_id} not written: {e}")


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

    if not plan.files:
        print(f"No changes to commit — skip commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"A1: no diff vs baseline at {stage_name}")
        return _finalize(
            commit_plan.CommitOutcome(commit_plan.OUTCOME_SKIPPED, "no changes to commit")
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
