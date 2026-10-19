"""R2.3 (roadmap R2): CandidatePlan + DeltaAssembler — ONE assembly of a unit.

Before R2.3 the unit's commit content was computed independently in two
places: :func:`awf.commit_plan._plan_split` (the file set + exclusions) and
the inline staging/merge inside :mod:`awf.commit_gate`. The roadmap's
architectural exit is one :class:`CandidatePlan` for preview, verify,
approval and publication, with :mod:`awf.commit_gate` as the coordinator.

This module is that single assembly point:

* :class:`DeltaAssembler` reads the change set through the R2.1 git model
  (:mod:`awf.infra.git_model`, byte-faithful NUL protocol) and turns it into
  TYPED operations — modified / added / deleted / renamed (a pair!) /
  copied / typechange / symlink — attributed against the baseline facts.
* :class:`CandidatePlan` records the assembly result: HEAD-expected, the
  baseline digest, the claimed transfers, the typed operations, the candidate
  tree OID, and every excluded / residual item WITH a reason.
* The delta-merge algorithm (REPORTS33 F1–F8: duplicate context, CRLF,
  empty, append, mode, ...) lives HERE now — the gate imports it, so the
  algorithm has exactly one home.

Ownership of a rename is a PAIR decision (roadmap R2.1/R2.3): both paths
owned → the source deletion joins the commit; both foreign → the pair is
excluded whole; a mixed pair is an explicit CONFLICT refusal, never a silent
split. An unsupported change (a submodule gitlink) is refused, not silently
degraded to the legacy path.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from ._errors import AwfApiError
from ._log import log as _log
from .infra import git_model

# Re-exported for callers: the ONE definitions live in awf.infra.git_model
# (REVIEW-M2 — diverging twins are forbidden, so these are imports, never a
# second literal or a second exception class).
from .infra.git_model import GIT_TIMEOUT as GIT_TIMEOUT
from .infra.git_model import GitModelError as GitModelError
from .infra.git_model import GitRepository

#: The display decode of raw path bytes (surrogateescape round-trips the
#: real bytes back — a name that is not valid UTF-8 is shown, never dropped).
_display = git_model.display_path


class DeltaError(AwfApiError):
    """Base class for a refused assembly of a unit candidate."""


class DeltaConflictError(DeltaError):
    """A change whose ownership cannot be decided without stealing foreign
    work (a rename whose two paths fall on opposite sides of the baseline).
    The candidate is refused — the caller must claim the pair explicitly or
    split the unit."""


# ─── git helper (isolated index) ─────────────────────────────────────────


def _git_isolated(
    project_dir: Path, args: list[str], env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run one git command — the ONE runner of the whole commit path (R-03).

    With ``env`` (``GIT_INDEX_FILE`` set) every index operation stays inside
    the throwaway index, so the user's ``.git/index`` is never opened. With
    ``env=None`` the command inherits the environment and reads the REAL
    index — the named, content-verified lookups (``publish``) use that form
    instead of growing a second runner.
    """
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        capture_output=True,
        text=True,
        # Hook output is arbitrary bytes (a non-UTF-8 file name echoed to
        # stderr is legal). Strict UTF-8 decoding raised UnicodeDecodeError
        # (a ValueError) past every typed publication boundary (R2.5 REVIEW,
        # iter 10, F3); surrogateescape keeps the bytes losslessly.
        errors="surrogateescape",
        check=False,
        timeout=GIT_TIMEOUT,
        env=env,
    )


def _worktree_blob(project_dir: Path, path_b: bytes) -> str | None:
    """The blob oid Git will store for ``path_b`` — DELEGATED to the R2.1
    model (REVIEW-M3: one worktree-content rule, one place).

    A symlink hashes its link TARGET STRING — never the bytes behind the link
    (REVIEW-F3). A directory / gitlink raises the model's refusal here too,
    instead of the old second implementation that silently returned ``None``
    (the F-QA2/QA4 latent divergence). ``None`` when the path vanished (best
    effort) — the caller keeps its fallback fact.
    """
    _mode, blob = GitRepository(project_dir).worktree_mode_blob(path_b)
    return blob


# ─── baseline facts (byte-faithful reads) ────────────────────────────────


def _quoted_end(s: str, start: int) -> int:
    """The index just past the closing quote of the C-quoted token at
    ``start`` (which must point at its opening ``"``), skipping ``\\``-escaped
    characters. The end of the string when the quote is unterminated."""
    i = start + 1
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        i += 1
    return n


def _short_sides(payload: str) -> list[str]:
    """Split a ``git status --short`` payload (the text after ``XY ``) into
    its PRINTED sides, positionally.

    A rename/copy line carries ``ORIG -> DEST`` and yields two sides; every
    other line yields one. The split is QUOTE-AWARE (REVIEW F-arrow): git
    C-quotes a name that itself contains the separator `` -> `` (or is not
    plain ASCII), so the arrow INSIDE a quoted side is part of the name, never
    a boundary. The tokens are returned as printed — the caller decides
    whether to keep that form or bridge it to the raw identity with
    :func:`_raw_identity`."""
    payload = payload.rstrip("\n")
    if not payload:
        return []
    if payload[0] == '"':
        end = _quoted_end(payload, 0)
        first = payload[:end]
        tail = payload[end:]
        if tail.startswith(" -> "):
            return [first, tail[4:]]
        return [first]
    if " -> " in payload:
        old, new = payload.split(" -> ", 1)
        return [old, new]
    return [payload]


def _baseline_file(project_dir: Path, todo_id: str, suffix: str) -> Path:
    return paths.context_dir(project_dir) / f"BASELINE-{todo_id}.{suffix}"


def _raw_identity(printed: str) -> str:
    """REVIEW-H1: bridge a PRINTED ``git`` path to the raw display identity.

    The baseline snapshots keep git's printed form (the R2.3 writer contract);
    under the default ``core.quotePath=true`` a non-ASCII name arrives
    C-quoted (``"\\321\\204..."``). The R2.1 model hands back the RAW path,
    so comparing raw to printed silently loses the match — a foreign staged
    rename on a non-ASCII name would then read as OWNED (S2-01 broken). This
    decodes the C-quoted form back to the string :func:`display_path` would
    produce for the raw bytes, so ownership compares identity to identity. A
    line that is not quoted (ASCII, or an already-raw name under
    ``core.quotePath=false``) is returned unchanged.
    """
    if len(printed) < 2 or printed[0] != '"' or printed[-1] != '"':
        return printed
    body = printed[1:-1]
    out = bytearray()
    simple = {
        "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
        "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C,
    }
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            nxt = body[i + 1]
            if "0" <= nxt <= "7":
                j = i + 1
                digits = ""
                while j < n and len(digits) < 3 and "0" <= body[j] <= "7":
                    digits += body[j]
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(simple.get(nxt, ord(nxt)))
            i += 2
            continue
        out.extend(ch.encode("utf-8", "surrogateescape"))
        i += 1
    return out.decode("utf-8", "surrogateescape")


def raw_lines(text: str) -> list[str]:
    """A printed git listing (one path per line — ``--name-only``,
    ``ls-files``) bridged to RAW display identities, in order.

    The SAME identity the R2.1 model and the baseline readers speak, so a
    path read live and a path read from a baseline snapshot compare as equal
    (REVIEW F-arrow: identity space must be uniform, or a name with an inner
    `` -> `` / a non-ASCII name silently mismatches). Blanks are dropped.
    """
    return [
        _raw_identity(line.strip())
        for line in text.splitlines()
        if line.strip()
    ]


def read_worktree_snapshot(project_dir: Path, todo_id: str) -> str:
    """The ``BASELINE-{todo_id}.worktree`` content — the sha of the worktree
    snapshot ``create_baseline`` took at dispatch time.

    "" when the file is absent or empty: a legacy baseline, a non-git
    project, a clean tree at dispatch, or a failed snapshot. Without it the
    delta mechanism is unavailable and the legacy behavior holds.
    """
    if not todo_id:
        return ""
    snap_file = _baseline_file(project_dir, todo_id, "worktree")
    if not snap_file.is_file():
        return ""
    try:
        return snap_file.read_text(encoding="utf-8").strip().split("\n")[0]
    except OSError:
        return ""


def read_baseline_status(project_dir: Path, todo_id: str) -> str | None:
    """The raw ``BASELINE-{todo_id}.status`` text, or None when absent."""
    if not todo_id:
        return None
    status_file = _baseline_file(project_dir, todo_id, "status")
    if not status_file.is_file():
        return None
    try:
        return status_file.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return None


def staged_at_baseline(project_dir: Path, todo_id: str) -> set[str] | None:
    """TODO-0195: the paths the index already held at baseline time.

    ``BASELINE-{todo_id}.status`` is snapshotted by ``create_baseline``; an
    entry whose index column (XY: X) is neither blank nor ``?`` was STAGED
    at dispatch. Only those are the unit's "foreign WIP".

    None when the snapshot is absent or unreadable (a legacy baseline): the
    caller falls back to the legacy rule and marks the plan.
    """
    text = read_baseline_status(project_dir, todo_id)
    if text is None:
        return None
    staged: set[str] = set()
    for line in text.splitlines():
        if len(line) < 4:
            continue
        if line[0] in (" ", "?"):
            continue  # clean or untracked — not in the index
        sides = _short_sides(line[3:])
        if sides:
            staged.add(_raw_identity(sides[-1]))
    return staged


def worktree_dirty_at_baseline(project_dir: Path, todo_id: str) -> set[str] | None:
    """REPORTS33 (TODO-0204): the paths whose WORKTREE column was dirty at
    baseline time — unstaged tracked changes (`` M``, `` D``, ...) that
    pre-date the unit.

    Complementary to :func:`staged_at_baseline` (the INDEX column): an
    ``MM`` entry is foreign staged WIP (0195 rules it), a ``M `` entry is
    clean in the worktree. None when the snapshot is absent (legacy).
    """
    text = read_baseline_status(project_dir, todo_id)
    if text is None:
        return None
    dirty: set[str] = set()
    for line in text.splitlines():
        if len(line) < 4:
            continue
        if line[0] != " ":
            continue  # staged (foreign WIP — 0195) or untracked ("?")
        if line[1] == " ":
            continue  # clean
        sides = _short_sides(line[3:])
        if sides:
            dirty.add(_raw_identity(sides[-1]))
    return dirty


def baseline_dirty_paths(project_dir: Path, todo_id: str) -> set[str] | None:
    """R2.3: EVERY path the baseline carried as dirty — the ownership set.

    A path the baseline already had dirty is pre-existing work the unit does
    not own. Unlike :func:`staged_at_baseline` (the index column only), this
    is the full footprint: both the index and the worktree columns, AND BOTH
    MEMBERS OF A STAGED RENAME PAIR (``R  old -> new`` makes ``old`` and
    ``new`` dirty, not just the destination). That pair fact is what lets the
    assembler exclude a FOREIGN staged rename whole (S2-01) instead of
    deleting the source and adding the destination.

    None when the snapshot is absent (legacy baseline).
    """
    text = read_baseline_status(project_dir, todo_id)
    if text is None:
        return None
    dirty: set[str] = set()
    for line in text.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        if xy == "??":
            continue
        if xy[0] == " " and xy[1] == " ":
            continue
        for side in _short_sides(line[3:]):
            dirty.add(_raw_identity(side))
    return dirty


def read_baseline_untracked(project_dir: Path, todo_id: str) -> set[str] | None:
    """The ``BASELINE-{todo_id}.untracked`` snapshot (pre-existing untracked
    paths) in RAW identity (REVIEW-H1: the printed C-quoted form is decoded,
    so a non-ASCII path matches the model's raw path), or None when the file
    is missing."""
    if not todo_id:
        return None
    f = _baseline_file(project_dir, todo_id, "untracked")
    if not f.is_file():
        return None
    try:
        return {
            _raw_identity(line.strip())
            for line in f.read_text(
                encoding="utf-8", errors="surrogateescape"
            ).splitlines()
            if line.strip()
        }
    except OSError:
        return None


def read_include_list(project_dir: Path, todo_id: str) -> list[str]:
    """The ``BASELINE-{todo_id}.include`` paths (explicitly included
    pre-existing untracked), or [].

    One reader for the fact: the canonical byte-faithful reader in
    :mod:`awf.include_untracked` is delegated to (it returns None when the
    file is absent — normalized here to []). A second parser of the same
    file would drift from the include audit's writer.
    """
    if not todo_id:
        return []
    from .include_untracked import read_include_list as _canonical

    try:
        return list(_canonical(project_dir, todo_id) or [])
    except Exception:
        return []


# ─── the raw file-set pass (moved from commit_plan — one home) ───────────


def _legacy_plan_split(
    project_dir: Path, baseline_sha: str, todo_id: str = ""
) -> PlanSplit:
    """The pre-R2.3 file-set pass (baseline diff + untracked filter).

    The RAW set the :class:`DeltaAssembler` enriches with typed rename
    ownership (:meth:`DeltaAssembler._typed_operations`) and an explicit
    claim. Moved here from ``commit_plan`` (R2.3 architect-review): the
    file-set computation belongs to the assembly module — the plan-level
    :func:`awf.commit_plan._plan_split` calls in, never the reverse (one
    home, no commit_plan <-> commit_delta callback). Behavior is unchanged
    from the R-03 / TODO-0195 / REPORTS33 implementation; ``commit_plan``
    re-exports the name for its plan surface.
    """
    if not baseline_sha:
        return PlanSplit(files=[])

    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", baseline_sha],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,  # AUD04-11: hung git must not hang the commit gate
        )
        if result.returncode != 0:
            logs_dir = project_dir / ".agentic" / "logs"
            if logs_dir.is_dir():
                _log(
                    logs_dir,
                    f"WARNING: git diff vs baseline {baseline_sha!r} failed "
                    f"(rc={result.returncode}): {result.stderr.strip()}",
                )
            return PlanSplit(files=[])
        modified_raw = raw_lines(result.stdout)
    except (subprocess.SubprocessError, OSError):
        return PlanSplit(files=[])

    try:
        staged_head = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,  # AUD04-11
        )
    except (subprocess.SubprocessError, OSError):
        return PlanSplit(files=[])  # can't tell what is staged — no plan, no commit
    if staged_head.returncode != 0:
        logs_dir = project_dir / ".agentic" / "logs"
        if logs_dir.is_dir():
            _log(
                logs_dir,
                f"WARNING: git diff --cached failed "
                f"(rc={staged_head.returncode}): {staged_head.stderr.strip()}",
            )
        return PlanSplit(files=[])
    staged_vs_head = set(raw_lines(staged_head.stdout))

    baseline_staged = staged_at_baseline(project_dir, todo_id)
    legacy = baseline_staged is None
    if legacy:
        # No baseline status snapshot (legacy/auto baseline): the old rule
        # — subtract everything the index staged vs the baseline NOW.
        try:
            foreign = subprocess.run(
                ["git", "diff", "--cached", "--name-only", baseline_sha],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_TIMEOUT,  # AUD04-11
            )
        except (subprocess.SubprocessError, OSError):
            return PlanSplit(files=[])
        if foreign.returncode != 0:
            logs_dir = project_dir / ".agentic" / "logs"
            if logs_dir.is_dir():
                _log(
                    logs_dir,
                    f"WARNING: git diff --cached vs baseline {baseline_sha!r} failed "
                    f"(rc={foreign.returncode}): {foreign.stderr.strip()}",
                )
            return PlanSplit(files=[])
        foreign_paths = set(raw_lines(foreign.stdout))
        if todo_id:
            _log(
                project_dir / ".agentic" / "logs",
                f"TODO-0195: no BASELINE-{todo_id}.status snapshot (legacy "
                "baseline) — the whole staged-vs-baseline set is treated "
                "as foreign (old rule)",
            )
    else:
        foreign_paths = baseline_staged

    modified = [p for p in modified_raw if p not in foreign_paths]
    worker_staged = sorted(staged_vs_head - foreign_paths)

    # REPORTS33 (TODO-0204): the delta mechanism. A file tracked-dirty at
    # dispatch pre-dates the unit: the commit may carry only the unit's
    # own edit (worktree vs the dispatch-time worktree snapshot). A file
    # the unit never touched since the dispatch has an EMPTY delta — it
    # is excluded from the plan and stays dirty in the worktree (the
    # dispatch warning's promise, made true). The mechanism needs both
    # snapshots (``.status`` + ``.worktree``); on absence (legacy
    # baseline, clean tree at dispatch, git failure) the legacy behavior
    # holds — the whole diff enters the commit, as before.
    delta_files: list[str] = []
    no_delta: set[str] = set()
    snap_sha = read_worktree_snapshot(project_dir, todo_id)
    dirty_at_baseline = worktree_dirty_at_baseline(project_dir, todo_id)
    if snap_sha and dirty_at_baseline:
        try:
            snap_diff = subprocess.run(
                ["git", "diff", "--name-only", snap_sha],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=GIT_TIMEOUT,  # AUD04-11: hung git must not hang the commit gate
            )
        except (subprocess.SubprocessError, OSError):
            snap_diff = None
        if snap_diff is not None and snap_diff.returncode == 0:
            touched_since_snap = set(raw_lines(snap_diff.stdout))
            for p in modified:
                if p not in dirty_at_baseline:
                    continue
                if p in touched_since_snap:
                    delta_files.append(p)
                else:
                    no_delta.add(p)
            modified = [p for p in modified if p not in no_delta]

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
            timeout=GIT_TIMEOUT,  # AUD04-11
        )
        if untracked_result.returncode == 0:
            untracked_now = raw_lines(untracked_result.stdout)
        else:
            untracked_now = []
    except (subprocess.SubprocessError, OSError):
        untracked_now = []

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

    pre_existing: set[str] = set()
    if baseline_untracked_path and baseline_untracked_path.exists():
        try:
            pre_existing = set(
                raw_lines(
                    baseline_untracked_path.read_text(
                        encoding="utf-8", errors="surrogateescape"
                    )
                )
            )
            untracked_new = [f for f in untracked_now if f not in pre_existing]
        except OSError:
            untracked_new = untracked_now  # best effort — include all untracked
    else:
        untracked_new = untracked_now

    # Combine + dedupe (a file could be in both lists if it was deleted
    # then re-created). Order: modified first, then new untracked.
    seen: set[str] = set()
    combined: list[str] = []
    for path in [*modified, *untracked_new]:
        if path and path not in seen:
            seen.add(path)
            combined.append(path)

    # TODO-0195: what the plan left outside (with the reason) — the raw
    # baseline-diff set minus the commit set. REVIEW-F1: the reason is
    # precise in BOTH branches — a path excluded because it is foreign
    # staged (foreign_paths — baseline-anchored or the legacy staged-vs-
    # baseline proxy) is "staged-at-baseline"/"staged" respectively;
    # everything else left outside is a pre-existing untracked file.
    files_set = set(combined)
    excluded: list[tuple[str, str]] = []
    for path in dict.fromkeys([*modified_raw, *untracked_now]):
        if not path or path in files_set:
            continue
        if path in (foreign_paths or ()):
            reason = "staged" if legacy else "staged-at-baseline"
        elif path in no_delta:
            reason = "pre-dispatch changes only (no unit delta)"
        else:
            reason = "pre-existing untracked"
        excluded.append((path, reason))

    return PlanSplit(
        files=combined,
        excluded=sorted(excluded),
        worker_staged=worker_staged,
        legacy_fallback=legacy,
        delta_files=sorted(delta_files),
    )


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
    baseline path). Empty list on error. Moved here from ``commit_plan``
    (R2.3 architect-review) with its sibling ``_legacy_plan_split``: the
    file-set computation lives in the assembly module now.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,  # AUD04-11
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


# ─── the delta-merge algorithm (moved from commit_gate — one home) ───────


def parse_delta_hunks(
    patch_text: str,
) -> list[tuple[int, list[tuple[str, str]]]] | None:
    """The ``@`` hunks of a unified diff as ``(base_pos, ops)`` pairs, or
    None when the text is not a parseable text diff (binary, garbage).

    ``base_pos`` — the 0-based position in the BASE content the hunk is
    anchored at; ``ops`` — ``(op, text)`` lines with op in ``{' ', '-', '+'}``
    (a blank context line arrives as ``("", "")``, normalized to ``(" ",
    "")``). A garbage line returns None — the caller degrades loudly, never
    guesses a diff shape.
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
                in_hunk = False
            elif line == "":
                hunks[-1][1].append((" ", ""))
            elif line[:1] in (" ", "-", "+"):
                hunks[-1][1].append((line[:1], line[1:]))
            elif line.startswith("\\"):
                continue
            else:
                return None
    if not hunks or not any(hunk for _, hunk in hunks):
        return None
    return hunks


def head_position(
    snap_line: int,
    pre_hunks: list[tuple[int, list[tuple[str, str]]]],
) -> int:
    """The 0-based position in HEAD of the snapshot line ``snap_line``
    (QA F1, REVIEW 2026-10-12: the exact hunk anchor in HEAD coordinates).

    ``pre_hunks`` — the parsed snapshot→HEAD diff (:func:`parse_delta_hunks`):
    insertions ('+') before the line raise its position in HEAD, deletions
    ('-') before it lower it. A pre-dispatch hunk that removes the line
    itself still yields a position (the line is absent from HEAD — the
    caller's verbatim match then fails and degrades); a hunk that merely
    surrounds the line is exact.
    """
    shift = 0
    for base_pos, ops in pre_hunks:
        if base_pos > snap_line:
            continue
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
            shift += delta
    return snap_line + shift


def head_end_position(
    snap_end: int,
    pre_hunks: list[tuple[int, list[tuple[str, str]]]],
) -> int:
    """The 0-based insertion position in HEAD of the snapshot BOUNDARY
    ``snap_end`` (the unit's append point; QA F8, REVIEW 2026-10-12).

    A boundary sits BETWEEN lines, unlike a line in :func:`head_position`: a
    pre-dispatch insertion AT or before the boundary pushes the HEAD boundary
    past the inserted lines (the unit appended after them); a pre-dispatch
    deletion of a line BEFORE the boundary pulls it back. Context lines
    before the boundary are in both files and shift nothing.
    """
    shift = 0
    for base_pos, ops in pre_hunks:
        if base_pos > snap_end:
            continue
        pos = base_pos
        for op, _text in ops:
            if op == "+":
                if pos <= snap_end:
                    shift += 1
            else:
                if op == "-" and pos < snap_end:
                    shift -= 1
                pos += 1
    return snap_end + shift


def apply_unit_delta(
    head_text: str,
    patch_text: str,
    pre_hunks: list[tuple[int, list[tuple[str, str]]]],
) -> str | None:
    """Apply the unit's edit (the snapshot→worktree diff) to the HEAD
    content — the content the unit commit must carry.

    The full REPORTS33 F1–F8 contract (moved here from ``commit_gate``; keep
    every clause — each one closes a silent-corruption class):

    * F1/F3 (REVIEW iter 4): the anchor is computed EXACTLY in HEAD
      coordinates (:func:`head_position` shifts the snapshot position by the
      pre-dispatch insertions/deletions before it) and ONLY the exact anchor
      is accepted. The fuzz of ≤2 may only trim EDGE context; the inner
      context and the deleted lines must match verbatim. An unbounded scan
      (F1) or a window scan from ``anchor - window`` (F3) let a duplicated
      context capture the edit far from its real home — that search was
      removed outright: it was unreachable for placeable hunks and could only
      fire on an unplaceable one, where matching anywhere is corruption.
    * F4: a hunk with no base side (``@@ -0,0`` — the file was emptied before
      the dispatch) has no anchor in HEAD; splicing at the negative index
      would leak HEAD lines into the commit. Never placed → None.
    * F5: a hunk whose base side is ALL context (empty seq after the fuzz
      drop) is never placed — its old placement at ``min(base_pos + shift,
      len(target))`` was prepend semantics, ignoring both "the addition comes
      AFTER the context" and the pre-dispatch shift.
    * F7: CRLF is never merged — ``splitlines()`` eats the ``\r`` and the
      join reassembles with LF, silently normalizing the whole file (phantom
      dirt after the commit). Any ``\r`` in ``head_text``/``patch_text`` →
      None.
    * F8: an append hunk (the unit's '+' lines at the END, no base-side
      content after them) is spliced at the HEAD coordinate of the hunk END
      (:func:`head_end_position`), never at ``anchor + len(seq)`` of the
      matched fuzz variant — a variant that dropped trailing context, or a
      pre-dispatch line added after the window, would otherwise move the
      unit's lines to the start of the window (F8a/F8b/F8c), silently.

    Returns the merged content, or None when a hunk cannot be placed
    (overlap, or no anchor) or the content is CRLF — the caller degrades to
    the legacy full-worktree commit, loudly.
    """
    if "\r" in head_text or "\r" in patch_text:
        return None
    hunks = parse_delta_hunks(patch_text)
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
        first_plus = next((i for i, (op, _t) in enumerate(hunk) if op == "+"), None)
        is_append = first_plus is not None and all(op == "+" for op, _t in hunk[first_plus:])
        placed: tuple[int, int, list[str]] | None = None
        for lead in range(0, min(2, lead_ctx) + 1):
            for trail in range(0, min(2, trail_ctx) + 1):
                if lead + trail > 2:
                    continue
                end = len(expected) - trail if trail else len(expected)
                seq = [text for _, text in expected[lead:end]]
                if not seq:
                    continue
                anchor = head_position(base_pos + lead, pre_hunks) + shift
                if anchor < 0 or target[anchor:anchor + len(seq)] != seq:
                    continue
                if is_append:
                    ins = head_end_position(base_pos + len(expected), pre_hunks) + shift
                    if ins < anchor + len(seq):
                        continue
                    e = 0
                    kept: list[str] = []
                    for op, text in hunk:
                        if op == "+":
                            continue
                        if op == " " and lead <= e < end:
                            kept.append(text)
                        e += 1
                    plus = [text for _op, text in hunk[first_plus:]]
                    out = kept + target[anchor + len(seq) : ins] + plus
                    placed = (anchor, ins - anchor, out)
                else:
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


# ─── staging one delta file (moved from commit_gate) ─────────────────────


def stage_delta_file(
    project_dir: Path,
    env: dict[str, str],
    file: str,
    snap_sha: str,
    logs_dir: Path | None,
    degraded: list[str] | None = None,
) -> None:
    """Commit ONLY the unit's own edit of a tracked-dirty-at-baseline file
    into the throwaway index (REPORTS33, TODO-0204).

    On a placeable failure (overlap, binary, CRLF line endings, a symlink,
    any git error) the file degrades to the legacy full-worktree commit —
    loudly logged, never silent.

    R2.4 (roadmap R2.3-4): ``degraded`` — when a full-file degradation
    happens, the path is appended so the CandidatePlan can BIND the fact
    (a candidate digest that includes the degraded paths). The full file
    then enters the commit only by an explicit approval tied to that
    digest; a bare warning is never consent.

    The ``--no-renames`` on the two per-file diffs is DELIBERATE, not the
    old gate's global policy: each runs with an explicit ``-- <file>``
    pathspec, where rename PAIRING cannot apply. Ownership of a rename is a
    PAIR decision made once by the R2.1 model in
    :meth:`DeltaAssembler._typed_operations` (roadmap R2.1/R2.3) — never
    re-derived here from a per-file diff. One policy, one owner
    (roadmap добор 0220 п.1).
    """
    def _warn(msg: str) -> None:
        print(f"WARNING: {msg}", file=sys.stderr)
        if logs_dir is not None:
            _log(logs_dir, msg)

    def _mark_degraded() -> None:
        if degraded is not None and file not in degraded:
            degraded.append(file)

    def _degrade(why: str) -> None:
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: {why} — the full "
            "worktree content enters the unit commit (legacy behavior)"
        )
        _mark_degraded()
        add = _git_isolated(project_dir, ["add", "--", file], env)
        if add.returncode != 0:
            _warn(
                f"REPORTS33 delta fallback add for {file!r} failed "
                f"(rc={add.returncode}): "
                f"{add.stderr.strip() or add.stdout.strip()}"
            )

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
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: the file is missing "
            "in the worktree (a deletion) — the worktree state is "
            "committed as-is"
        )
        _git_isolated(project_dir, ["add", "--", file], env)
        return
    if snap_blob is None:
        _warn(
            f"REPORTS33 delta mechanism for {file!r}: the file is absent "
            "from the dispatch-time snapshot (deleted pre-dispatch) — the "
            "whole worktree content is committed as the unit's edit"
        )
        _mark_degraded()
        _git_isolated(project_dir, ["add", "--", file], env)
        return
    if unit_blob == snap_blob:
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
        timeout=GIT_TIMEOUT,
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
        _degrade(f"the snapshot→worktree diff failed (rc={diff_r.returncode})")
        return
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
    pre_hunks = parse_delta_hunks(pre_r.stdout)
    if pre_hunks is None:
        _degrade("the snapshot→HEAD diff is not a parseable text diff")
        return
    if "\r" in head_text or "\r" in diff_r.stdout:
        _degrade(
            "CRLF line endings in the HEAD content or the unit diff — "
            "the text merge would normalize them to LF (line endings) — "
            "no clean unit delta"
        )
        return
    merged = apply_unit_delta(head_text, diff_r.stdout, pre_hunks)
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
        timeout=GIT_TIMEOUT,
        env=env,
    )
    if blob_r.returncode != 0:
        _degrade("the merged content could not be written as a blob")
        return
    mode_r = _git_isolated(project_dir, ["ls-tree", "HEAD", "--", file], env)
    if mode_r.returncode != 0:
        _degrade(f"the HEAD mode could not be read (rc={mode_r.returncode})")
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


def _has_head(project_dir: Path) -> bool:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def head_sha(project_dir: Path) -> str:
    """The current HEAD sha, or "" when there is no commit yet."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def stage_plan_into_index(
    project_dir: Path,
    env: dict[str, str],
    files: list[str] | tuple[str, ...],
    delta_files: list[str] | tuple[str, ...],
    todo_id: str,
    logs_dir: Path | None,
    degraded: list[str] | None = None,
) -> str:
    """Stage the plan's files into the throwaway index (one staging home).

    ``read-tree`` from HEAD (when commits exist), ``add`` the plain files,
    and commit ONLY the unit delta of each ``delta_files`` entry
    (:func:`stage_delta_file`). Returns "" on success, the error text
    otherwise — the caller (gate or assembler) decides the outcome.

    ``degraded`` (R2.4): when given, the paths that degraded to their full
    worktree content are appended — the CandidatePlan binds the fact.
    """
    delta = set(delta_files)
    plain = [f for f in files if f not in delta]
    try:
        init = _git_isolated(project_dir, ["read-tree", "--empty"], env)
        if init.returncode != 0:
            return (
                f"git read-tree --empty failed (rc={init.returncode}): "
                f"{init.stderr.strip()}"
            )
        if _has_head(project_dir):
            read = _git_isolated(project_dir, ["read-tree", "HEAD"], env)
            if read.returncode != 0:
                return (
                    f"git read-tree HEAD failed (rc={read.returncode}): "
                    f"{read.stderr.strip()}"
                )
        if plain:
            add = _git_isolated(project_dir, ["add", "--", *plain], env)
            if add.returncode != 0:
                return (
                    f"git add failed (rc={add.returncode}): "
                    f"{add.stderr.strip() or add.stdout.strip()}"
                )
        if delta:
            snap_sha = read_worktree_snapshot(project_dir, todo_id)
            if not snap_sha:
                if degraded is not None:
                    degraded.extend(f for f in sorted(delta) if f not in degraded)
                add = _git_isolated(project_dir, ["add", "--", *sorted(delta)], env)
                if add.returncode != 0:
                    return (
                        f"git add failed (rc={add.returncode}): "
                        f"{add.stderr.strip() or add.stdout.strip()}"
                    )
            else:
                for file in sorted(delta):
                    stage_delta_file(
                        project_dir, env, file, snap_sha, logs_dir, degraded
                    )
    except (subprocess.SubprocessError, OSError) as e:
        return f"git staging failed: {e}"
    return ""


# ─── the candidate plan (typed) ──────────────────────────────────────────


@dataclass(frozen=True)
class CandidateOperation:
    """One TYPED change the unit may commit.

    ``raw_path`` / ``raw_old_path`` are the IDENTITY (display strings that
    round-trip the real bytes via ``surrogateescape``); ``kind`` mirrors the
    git model (modified/added/deleted/renamed/copied/typechange/untracked);
    ``mode`` is the git mode git will store (``120000`` for a symlink,
    ``100755`` for an executable, ``100644`` otherwise); ``blob`` is the
    staged blob oid when the model reported one. ``owned`` is the baseline
    attribution: False means the path pre-dates the unit.
    """

    raw_path: str
    kind: str
    raw_old_path: str | None = None
    mode: str | None = None
    blob: str | None = None
    owned: bool = True

    @property
    def is_symlink(self) -> bool:
        return self.mode == "120000"

    @property
    def paths(self) -> tuple[str, ...]:
        if self.raw_old_path is not None:
            return (self.raw_old_path, self.raw_path)
        return (self.raw_path,)


@dataclass(frozen=True)
class CandidatePlan:
    """R2.3: the full delta a unit will commit — ONE object for preview,
    verify, approval and publication (coordinator: the commit gate).

    - ``head_expected`` — the HEAD the assembly was computed against.
    - ``baseline_digest`` — a stable digest of the baseline facts read.
    - ``claimed`` — the raw paths adopted by an explicit claim / include
      (R2.2), so the plan shows WHY a pre-existing path is present.
    - ``operations`` — the typed owned operations.
    - ``conflicts`` — rename pairs with mixed ownership: NEVER included,
      surfaced as an explicit conflict.
    - ``candidate_tree`` — the tree oid of the isolated candidate index.
    - ``files`` — the exact commit content (relative display paths).
    - ``excluded`` / ``residual`` — what the plan leaves out, with a reason.
    - ``degraded`` (R2.4) — the delta files that degraded to their FULL
      worktree content while the candidate tree was built; the candidate
      digest binds them, so a full file enters the commit only by explicit
      approval tied to that digest (roadmap R2.3-4).
    """

    todo_id: str
    head_expected: str
    baseline_digest: str
    claimed: tuple[str, ...]
    operations: tuple[CandidateOperation, ...]
    candidate_tree: str
    files: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...] = ()
    residual: tuple[tuple[str, str], ...] = ()
    conflicts: tuple[CandidateOperation, ...] = ()
    worker_staged: tuple[str, ...] = ()
    legacy_fallback: bool = False
    delta_files: tuple[str, ...] = ()
    degraded: tuple[str, ...] = ()
    # F-QA-8 (R2.4): non-empty when the candidate tree could NOT be built
    # (a git staging / write-tree error). An empty ``candidate_tree`` is
    # ambiguous on its own — it is the legitimate "no files" state AND the
    # failure state — so the ERROR rides here and the mandatory-check side
    # refuses instead of silently skipping the check (a fail-open).
    build_error: str = ""


@dataclass(frozen=True)
class PlanSplit:
    """TODO-0195: one pass over the tree → the unit's commit set and what
    it leaves outside. Kept for the commit-plan surface (same fields as
    before R2.3); the assembler fills it.
    """

    files: list[str]
    excluded: list[tuple[str, str]] = field(default_factory=list)
    worker_staged: list[str] = field(default_factory=list)
    legacy_fallback: bool = False
    delta_files: list[str] = field(default_factory=list)
    #: R2.3: a model refusal (unsupported change / unmerged / sparse / split,
    #: or a mixed-ownership rename pair) blocks the plan — the gate turns it
    #: into OUTCOME_REFUSED, never a silent legacy commit. "" when usable.
    refusal: str = ""


def _baseline_digest(project_dir: Path, todo_id: str, baseline_sha: str) -> str:
    """A stable digest of the baseline facts the assembly read."""
    h = hashlib.sha256()
    h.update(b"baseline\0" + baseline_sha.encode("ascii") + b"\0")
    for suffix in ("status", "untracked", "worktree", "include"):
        f = _baseline_file(project_dir, todo_id, suffix) if todo_id else None
        h.update(b"\0" + suffix.encode("ascii") + b"\0")
        if f is not None and f.is_file():
            try:
                h.update(f.read_bytes())
            except OSError:
                pass
    return h.hexdigest()


class DeltaAssembler:
    """R2.3: the ONE assembly of a unit's candidate.

    Reads the change set through the R2.1 git model, attributes ownership
    from the baseline facts, groups a rename into a PAIR, and materialises
    the candidate tree in an isolated index. The result is a
    :class:`CandidatePlan`. A change the model cannot represent (a submodule
    gitlink, an unmerged/sparse/split index) raises the model's refusal; a
    mixed-ownership pair raises :class:`DeltaConflictError` — never a silent
    legacy commit.
    """

    def __init__(
        self,
        project_dir: Path,
        todo_id: str,
        baseline_sha: str = "",
        claim: tuple[str, ...] | list[str] = (),
        logs_dir: Path | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.todo_id = todo_id
        self.baseline_sha = baseline_sha
        self.claim = tuple(claim)
        self.logs_dir = logs_dir or (self.project_dir / ".agentic" / "logs")

    # -- typed operations + ownership -----------------------------------

    def _typed_operations(
        self, adopted: set[str]
    ) -> tuple[
        list[CandidateOperation],
        list[CandidateOperation],
        list[CandidateOperation],
    ]:
        """The typed operations, the mixed-pair conflicts, the foreign pairs.

        Reads the model's change set (byte-faithful) and attributes each
        operation against the baseline dirty set: a path dirty at baseline is
        foreign. ``adopted`` (a claim / include) owns a path even when the
        baseline carried it dirty. A rename is ONE pair decision
        (:meth:`_path_owned` on both members): both owned → the pair is owned;
        both foreign → the pair is FOREIGN (returned so the assembler can
        exclude BOTH members — REVIEW iter 6); a MIXED pair is a conflict
        (returned separately, never owned).

        The foreign pair must NOT be dropped here: the legacy file-set pass
        sees only the destination in the baseline status snapshot, so when the
        destination was edited after ``git mv`` (similarity below the rename
        threshold) the worktree diff degrades to add+delete and the legacy
        pass leaks the SOURCE as a deletion into the commit — the S2-01 theft.
        The pair fact the model holds decides ownership, so it leaves here.
        """
        repo = GitRepository(self.project_dir)
        changes = repo.changes()
        baseline_dirty = baseline_dirty_paths(self.project_dir, self.todo_id)
        # REVIEW-F2: a path already UNTRACKED at baseline is pre-existing work
        # the unit does not own — the dirty set above deliberately skips the
        # ``??`` rows, so the untracked snapshot is the second foreign source.
        pre_existing_untracked = read_baseline_untracked(self.project_dir, self.todo_id)

        owned: list[CandidateOperation] = []
        conflicts: list[CandidateOperation] = []
        foreign_ops: list[CandidateOperation] = []
        for r in changes:
            is_pair = r.kind == "renamed" and r.old_path is not None
            if is_pair:
                old_owned = self._path_owned(
                    r.old_path, adopted, baseline_dirty, pre_existing_untracked
                )
                new_owned = self._path_owned(
                    r.path, adopted, baseline_dirty, pre_existing_untracked
                )
                own = old_owned and new_owned
                mixed = old_owned != new_owned
            else:
                own = self._path_owned(
                    r.path, adopted, baseline_dirty, pre_existing_untracked
                )
                mixed = False
            mode, blob = self._stored_mode_blob(r)
            op = CandidateOperation(
                raw_path=_display(r.path),
                kind=r.kind,
                raw_old_path=_display(r.old_path) if r.old_path is not None else None,
                mode=mode,
                blob=blob,
                owned=own,
            )
            if mixed:
                conflicts.append(op)
            elif own:
                owned.append(op)
            elif is_pair:
                foreign_ops.append(op)
        return owned, conflicts, foreign_ops

    def _stored_mode_blob(self, r) -> tuple[str | None, str | None]:
        """The mode and blob Git will store for the change ``r`` (REVIEW-F3).

        The INDEX facts are stale for an unstaged worktree edit (the worker
        does not stage — hard rule), so the effective mode comes from the
        WORKTREE column: a ``chmod`` the unit would commit reports ``100755``,
        a regular→symlink reports ``120000``. The blob is the worktree content
        — for a symlink the TARGET STRING, never the bytes behind the link
        (``_worktree_blob``). A deletion carries no content blob; a record
        without worktree facts keeps the index/base facts (the old behavior).
        """
        if r.kind == "deleted":
            return (r.index_mode or r.base_mode), None
        if r.kind == "untracked":
            # An untracked record carries NO index/work modes (the model reads
            # it straight from the status ``?`` row), so the stored facts must
            # come from the worktree — the SAME rule as a tracked symlink: a
            # new symlink stores ``120000`` with its TARGET STRING, an
            # executable stores ``100755``, otherwise a plain ``100644`` blob.
            return self._untracked_mode_blob(r.path)
        mode = r.work_mode
        if mode in (None, "000000"):
            mode = r.index_mode or r.base_mode
        blob = r.index_blob
        if r.provenance == "index":
            work_blob = _worktree_blob(self.project_dir, r.path)
            if work_blob is not None:
                blob = work_blob
        return mode, blob

    def _untracked_mode_blob(self, path_b: bytes) -> tuple[str | None, str | None]:
        """The mode and blob Git will store for an UNTRACKED path (REVIEW-F3).

        DELEGATED to the R2.1 model (:meth:`GitRepository.worktree_mode_blob`,
        REVIEW-M3): the ``os.lstat`` type / owner-exec-bit rule and the link
        TARGET STRING for a symlink live in ONE place, so a directory / gitlink
        is REFUSED here exactly as the model refuses it — never a fabricated
        executable file entry (the F-QA2/QA4 class). ``(None, None)`` when the
        path vanished between the status read and here (best effort).
        """
        return GitRepository(self.project_dir).worktree_mode_blob(path_b)

    @staticmethod
    def _path_owned(
        path_b: bytes,
        adopted: set[str],
        baseline_dirty: set[str] | None,
        baseline_untracked: set[str] | None,
    ) -> bool:
        """Whether the unit owns ``path_b``: an adopted (claim / include) path
        is always owned; a path the baseline already carried — dirty tracked
        (index/worktree) OR pre-existing untracked — is foreign; a legacy
        baseline (no snapshots) owns everything."""
        shown = _display(path_b)
        if shown in adopted:
            return True
        if baseline_dirty is not None and shown in baseline_dirty:
            return False
        if baseline_untracked is not None and shown in baseline_untracked:
            return False
        return True

    # -- legacy file set (moved from commit_plan._plan_split) -----------

    def _legacy_split(self) -> PlanSplit:
        """The baseline-diff file set + exclusions (preserves the pre-R2.3
        semantics the gate and the sentinel rely on). RENAME enrichment is
        applied on top in :meth:`assemble`, so this stays the raw set."""
        return _legacy_plan_split(
            self.project_dir, self.baseline_sha, self.todo_id
        )

    # -- assembly -------------------------------------------------------

    def assemble(self, *, strict: bool = False, build_tree: bool = False) -> CandidatePlan:
        """Build the :class:`CandidatePlan`.

        ``strict`` — raise :class:`DeltaConflictError` on a mixed-ownership
        pair instead of recording it and excluding the pair. ``build_tree``
        — materialise the candidate tree oid in an isolated index (inert:
        no ref, no user index change).
        """
        head_expected = head_sha(self.project_dir) if _has_head(self.project_dir) else ""
        baseline_digest = _baseline_digest(
            self.project_dir, self.todo_id, self.baseline_sha
        )
        # A claim / include ADOPTS a path (R2.2): it is owned even if the
        # baseline carried it dirty. Computed once — owned and claimed read
        # the SAME set.
        adopted = set(self.claim) | set(read_include_list(self.project_dir, self.todo_id))
        claimed = tuple(sorted(adopted))

        owned: list[CandidateOperation] = []
        conflicts: list[CandidateOperation] = []
        foreign_ops: list[CandidateOperation] = []
        if self.baseline_sha:
            owned, conflicts, foreign_ops = self._typed_operations(adopted)

        if strict and conflicts:
            names = ", ".join(op.raw_path for op in conflicts)
            raise DeltaConflictError(
                f"rename pair(s) with mixed ownership cannot be isolated: "
                f"{names} — the unit may claim the pair explicitly or split "
                "the unit; refusing to steal foreign work"
            )

        if self.baseline_sha:
            split = self._legacy_split()
            files = list(split.files)
            excluded = list(split.excluded)
            worker_staged = list(split.worker_staged)
            legacy_fallback = split.legacy_fallback
            delta_files = list(split.delta_files)
        else:
            files = list(_all_changed_files(self.project_dir))
            excluded = []
            worker_staged = []
            legacy_fallback = False
            delta_files = []

        # R2.3 rename-pair enrichment: an OWNED rename must delete its source
        # (the old test must not stay — B01); a FOREIGN rename must include
        # neither member (S2-01); a mixed pair is a conflict (excluded whole).
        conflict_paths = {p for op in conflicts for p in op.paths}
        foreign_paths = {p for op in foreign_ops for p in op.paths}
        file_set = set(files)
        if owned or conflict_paths or foreign_paths:
            for op in owned:
                if op.kind == "renamed" and op.raw_old_path is not None:
                    if op.raw_old_path not in file_set:
                        files.append(op.raw_old_path)
                        file_set.add(op.raw_old_path)
                    if op.raw_path not in file_set:
                        files.append(op.raw_path)
                        file_set.add(op.raw_path)
            for cp in conflict_paths:
                while cp in files:
                    files.remove(cp)
                file_set.discard(cp)
                excluded.append((cp, "conflicting rename-pair ownership (mixed)"))
            # REVIEW iter 6 (S2-01): strip BOTH members of a FOREIGN pair from
            # the file set — the legacy pass sees only the destination in the
            # baseline snapshot and, when the destination was edited after
            # ``git mv``, leaks the SOURCE as a deletion. A member the legacy
            # pass already excluded keeps its original reason; a member it
            # missed (the source) is removed here and explained.
            excluded_names = {e[0] for e in excluded}
            for fp in sorted(foreign_paths):
                while fp in files:
                    files.remove(fp)
                file_set.discard(fp)
                if fp not in excluded_names:
                    excluded.append(
                        (fp, "foreign rename pair (staged-at-baseline)")
                    )
                    excluded_names.add(fp)

        # REVIEW-F1: an ADOPTED path (an explicit claim / include) is
        # committed as a FULL file — the claim IS the consent the "full file
        # only by explicit claim" rule requires. The legacy pass drops a
        # claimed tracked file with no unit delta ("no unit delta"); the
        # assembler must re-admit it, or the claim silently loses its blob.
        adopted_owned = sorted(
            {p for op in owned for p in op.paths if p in adopted}
        )
        for p in adopted_owned:
            if p not in file_set:
                files.append(p)
                file_set.add(p)
        if adopted_owned:
            dropped = set(adopted_owned)
            excluded = [e for e in excluded if e[0] not in dropped]
            # The claim adopts the predecessor's blob too, so a claimed file
            # must be committed WHOLE: the per-file delta would keep only the
            # unit's own edit and drop the predecessor's change (the very
            # blob the claim consented to). Drop it from the delta set.
            delta_files = [f for f in delta_files if f not in dropped]

        candidate_tree = ""
        build_error = ""
        degraded: list[str] = []
        if build_tree and files:
            candidate_tree, build_error = self._build_candidate_tree(
                files, delta_files, degraded
            )

        residual = [
            (p, r) for p, r in excluded if "conflict" in r or "no unit delta" in r
        ]
        return CandidatePlan(
            todo_id=self.todo_id,
            head_expected=head_expected,
            baseline_digest=baseline_digest,
            claimed=claimed,
            operations=tuple(owned),
            candidate_tree=candidate_tree,
            files=tuple(files),
            excluded=tuple(sorted(excluded)),
            residual=tuple(sorted(residual)),
            conflicts=tuple(conflicts),
            worker_staged=tuple(worker_staged),
            legacy_fallback=legacy_fallback,
            delta_files=tuple(delta_files),
            degraded=tuple(sorted(degraded)),
            build_error=build_error,
        )

    def _build_candidate_tree(
        self, files: list[str], delta_files: list[str], degraded: list[str] | None = None
    ) -> tuple[str, str]:
        """Write the candidate tree oid in an isolated index (inert).

        Returns ``(tree_oid, error)``: ``(oid, "")`` on success, ``("",
        error_text)`` when the staging or ``write-tree`` failed. F-QA-8: an
        empty oid alone cannot tell "no files" from "build failed" — the
        error rides out separately so the caller refuses instead of
        treating a broken build as an empty candidate.
        """
        import tempfile

        fd, index_path = tempfile.mkstemp(
            prefix=f"awf-candidate-{self.todo_id}-", suffix=".idx"
        )
        os.close(fd)
        env = {**os.environ, "GIT_INDEX_FILE": index_path}
        try:
            err = stage_plan_into_index(
                self.project_dir,
                env,
                files,
                delta_files,
                self.todo_id,
                self.logs_dir,
                degraded,
            )
            if err:
                return "", err
            tree = _git_isolated(self.project_dir, ["write-tree"], env)
            if tree.returncode != 0:
                return "", (
                    f"git write-tree failed (rc={tree.returncode}): "
                    f"{tree.stderr.strip() or tree.stdout.strip()}"
                )
            return tree.stdout.strip(), ""
        finally:
            try:
                os.unlink(index_path)
            except OSError:
                pass


def assemble_candidate(
    project_dir: Path,
    todo_id: str,
    baseline_sha: str = "",
    claim: tuple[str, ...] | list[str] = (),
    *,
    strict: bool = False,
    build_tree: bool = False,
) -> CandidatePlan:
    """Convenience wrapper: one :class:`DeltaAssembler` call."""
    return DeltaAssembler(
        project_dir, todo_id, baseline_sha, claim
    ).assemble(strict=strict, build_tree=build_tree)
