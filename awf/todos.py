"""Port of lib/todos.sh — shared helpers for enumerating TODOs."""
from __future__ import annotations

import re
from pathlib import Path

from . import paths
from ._names import unique_name
from .paths import handoff_dir
from .signals import short_id as _short_id

#: A ``## REVIEW-фикс`` section heading. Two spellings exist in the archive:
#: the canonical ``(супервизор, виток N)`` and the legacy ``## REVIEW-фикс 2``.
#: ONE vocabulary for every reader and writer of the round number — the
#: supervisor's appender and the RUN-REPORT counter must never disagree about
#: what a heading says (TODO-0231 architect: they did, on legacy headings).
_REVIEW_HEADING_RE = re.compile(r"^##\s+REVIEW-фикс[^\n]*$", re.MULTILINE)
_REVIEW_ROUND_RE = re.compile(r"виток\s+(\d+)")
_REVIEW_LEGACY_RE = re.compile(r"^##\s+REVIEW-фикс\s+(\d+)")


def review_headings(text: str) -> list[str]:
    """Every ``## REVIEW-фикс`` heading line, in file order."""
    return _REVIEW_HEADING_RE.findall(text or "")


def review_round(heading: str) -> int | None:
    """The round number a heading declares — None when it declares none.

    The canonical marker wins (``виток N`` anywhere in the heading); the
    legacy spelling is its leading number (``## REVIEW-фикс 2 (…)``). ``iter
    N`` is NEVER read: that is the pipeline's attempt counter, a different
    fact (TODO-0231 W1). A heading without a number is numbered by its
    position — the caller's fallback, not this function's.
    """
    m = _REVIEW_ROUND_RE.search(heading or "")
    if m:
        return int(m.group(1))
    m = _REVIEW_LEGACY_RE.match(heading or "")
    return int(m.group(1)) if m else None


def review_summary(text: str) -> tuple[int | None, int]:
    """The aggregated review-round facts: (highest declared round, sections).

    ``highest`` is None when no heading declares a number. The two readers
    need different arithmetic — the appender advances past the numbers, the
    RUN-REPORT shows the current round — so they take the SAME aggregate and
    differ only in the final step. A second comprehension beside the parser
    is where the two answers could drift again (architect TODO-0231).
    """
    sections = review_headings(text)
    rounds = [n for n in (review_round(h) for h in sections) if n is not None]
    return (max(rounds) if rounds else None, len(sections))


def read_todo_text(project_dir: str | Path, todo_id: str) -> str:
    """The unit's TODO source: the live inbox file, else the archive copy.

    The ONE reader for "what did this unit declare" — candidate evidence,
    prove-red's contract keys and the RUN-REPORT iteration count all ask the
    same question. A unit lives in exactly one place (in the inbox while
    live, under ``done/<id>/`` once archived). ``surrogateescape`` keeps a
    non-UTF-8 file byte-faithful instead of raising; an unreadable file
    degrades to "". A caller that must name the exact path it failed to read
    (verify-pack's contract log) opens the file itself — "" cannot say WHICH
    of the two places was unreadable.
    """
    for f in (
        paths.inbox(project_dir) / f"{todo_id}.md",
        paths.done_dir(project_dir) / todo_id / "TODO.md",
    ):
        if f.is_file():
            try:
                return f.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError:
                return ""
    return ""


def is_closed(inbox: Path, outbox: Path, todo_id: str) -> bool:
    """Return True if any closure signal exists (canonical OR legacy form).

    Canonical: DONE-TODO-NNNN.ready, BLOCKED-TODO-NNNN.ready, ACK-TODO-NNNN.ready
    Legacy:    DONE-NNNN.ready,       BLOCKED-NNNN.ready,       ACK-NNNN.ready
    """
    short = _short_id(todo_id)
    closure_patterns = [
        outbox / f"DONE-{todo_id}.ready",
        outbox / f"DONE-{short}.ready",
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"BLOCKED-{short}.ready",
        inbox / f"ACK-{todo_id}.ready",
        inbox / f"ACK-{short}.ready",
    ]
    return any(p.exists() for p in closure_patterns)


def has_progress(outbox: Path, todo_id: str) -> bool:
    """Return True if a non-empty PROGRESS file exists (canonical OR legacy)."""
    short = _short_id(todo_id)
    progress_patterns = [
        outbox / f"PROGRESS-{todo_id}.ready",
        outbox / f"PROGRESS-{short}.ready",
        outbox / f"PROGRESS-{todo_id}.md",
        outbox / f"PROGRESS-{short}.md",
    ]
    return any(p.exists() and p.stat().st_size > 0 for p in progress_patterns)


def list_active_todos(inbox: Path, outbox: Path) -> list[str]:
    """Return active TODO ids, highest-numbered first.

    Active = .ready exists, .md non-empty, no closure signal.
    """
    if not inbox.exists():
        return []

    candidates: list[tuple[int, str]] = []
    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem  # e.g. TODO-0042
        md_file = inbox / f"{todo_id}.md"
        if not md_file.is_file() or md_file.stat().st_size == 0:
            continue
        if is_closed(inbox, outbox, todo_id):
            continue
        short = _short_id(todo_id)
        try:
            num = int(short)
        except ValueError:
            num = 0
        candidates.append((num, todo_id))

    candidates.sort(key=lambda x: x[0], reverse=True)
    return [todo_id for _, todo_id in candidates]


def newest_active(project_root: str | Path) -> str:
    """Return the highest-numbered active TODO id, or empty string if none.

    Convenience wrapper around ``list_active_todos`` for callers that just
    need "the current TODO" without managing inbox/outbox paths themselves.
    Used by orchestrator and cmd_start.
    """
    from . import paths

    project_root = Path(project_root)
    inbox = paths.inbox(project_root)
    outbox = paths.outbox(project_root)
    active = list_active_todos(inbox, outbox)
    return active[0] if active else ""


def _suffixed_name(dest_dir: Path, name: str) -> str:
    """First free ``name-N.ext`` in ``dest_dir`` — never the base name.

    The base name is reserved for the incoming winner, so the search
    starts at the first suffix (shared mechanism: awf/_names.py, base
    name treated as pre-occupied).
    """
    return unique_name(dest_dir, name, reserve_base=True)


def archive_todo(project_dir: str | Path, todo_id: str) -> Path | None:
    """DF6-1: Move completed TODO files from inbox/outbox to done/{todo_id}/.

    After verify approve (ACK/APPROVE), call this to archive:
    - inbox/TODO-{id}.md        → done/{id}/TODO.md
    - inbox/TODO-{id}.ready     → deleted (signal consumed)
    - inbox/ACK-{id}.ready      → deleted
    - inbox/APPROVE-{id}.ready  → deleted
    - outbox/PROGRESS-{id}.md   → done/{id}/PROGRESS.md
    - outbox/DONE-{id}.md       → done/{id}/DONE.md
    - outbox/DONE-{id}.json     → done/{id}/DONE.json (U3 machine facts, U5)
    - outbox/DONE-{id}.ready    → deleted
    - outbox/TEST-RESULTS-{id}.log → done/{id}/TEST-RESULTS-{id}.log (AUD15-07)
    - outbox/BLOCKED-{id}.{md,json} → done/{id}/ (H2: dead-attempt report)
    - outbox/REVIEW-{id}.{md,json}  → done/{id}/ (H2: rejected-attempt report)
    - outbox/BLOCKED-{id}.ready     → deleted (signal consumed)

    Returns path to done/{todo_id}/ dir, or None if nothing to archive.
    Idempotent — safe to call multiple times.

    A-12 (audit 2026-09-25) + REVIEW F1: a file in done/{todo_id}/ is
    never overwritten. Identical content (the same unit re-archived)
    stays idempotent — the sources are consumed, the archive copy is
    kept, no suffixes. Different content is versioned: the existing
    archive copy moves under a unique suffix (the _unique_name
    mechanism from retire_todo), the new file takes the canonical name.
    Both reports survive, so the restore → re-run → re-archive cycle
    completes without a refusal (QA F1: a refusal here would kill the
    pipeline after a successful commit, pipeline_engine.py). The
    canonical name is reserved for the incoming winner: a displaced
    copy never takes the base name (REVIEW F2 — with two different
    sources on one slot the displaced one would otherwise land on the
    freed canonical name and be silently overwritten).
    """
    import shutil

    from . import paths

    project_dir = Path(project_dir)
    inbox_p = paths.inbox(project_dir)
    outbox_p = paths.outbox(project_dir)
    dest = paths.done_dir(project_dir) / todo_id

    # TODO-0227 (M3): the archive MOVE is a destructive managed operation.
    # A symlinked ``.agentic`` / ``done`` used to send the versioning and
    # ``shutil.move`` calls outside the project; the no-follow preflight
    # refuses BEFORE the first move (or the consumed-signal unlink below),
    # so a refused archive leaves both the project tree and any canary
    # outside untouched. The refusal crosses as ``OSError`` — the family the
    # archive callers (``publish.archive_after_receipt``) already guard.
    #
    # F-AR2 (architect-review): the SAME barrier now covers the SOURCES the
    # move consumes, not only the destination. A symlinked ``inbox`` /
    # ``outbox`` / ``handoff`` used to let ``unlink`` / ``shutil.move``
    # delete or move files OUTSIDE the project (the S9-01 class the inbox
    # cleaners already refuse). One preflight over all four surfaces, before
    # any read or side effect.
    from .infra import managed_fs

    root = managed_fs.agentic_root_for(dest.parent)
    for surface in (dest, inbox_p, outbox_p, handoff_dir(project_dir)):
        try:
            managed_fs.assert_no_symlink_escape(surface, root=root)
        except managed_fs.ManagedPathError as e:
            raise OSError(f"archive {todo_id} refused: {e}") from e

    # A-12 (audit 2026-09-25): collect every (src -> dst) move FIRST so
    # the versioning below sees the whole picture before anything is
    # moved or deleted — the original shutil.move loop overwrote done/<id>/
    # files as it went (on POSIX the destination is silently replaced).
    moves: dict[Path, list[Path]] = {}

    # TODO .md
    todo_md = inbox_p / f"{todo_id}.md"
    if todo_md.is_file():
        moves.setdefault(dest / "TODO.md", []).append(todo_md)

    # PROGRESS and DONE from outbox — canonical (TODO-NNNN) AND legacy
    # short (NNNN) form, mirroring is_closed/has_progress (AUD01-05).
    for prefix in ("PROGRESS", "DONE"):
        for ext in (".md", ".json"):
            for tid_variant in (todo_id, _short_id(todo_id)):
                src = outbox_p / f"{prefix}-{tid_variant}{ext}"
                if src.is_file():
                    moves.setdefault(dest / f"{prefix}{ext}", []).append(src)

    # AUD15-07: the test-results log used to pile up in outbox forever —
    # it is the DONE report of this TODO, so it archives with it.
    for tid_variant in (todo_id, _short_id(todo_id)):
        tr = outbox_p / f"TEST-RESULTS-{tid_variant}.log"
        if tr.is_file():
            moves.setdefault(dest / f"TEST-RESULTS-{tid_variant}.log", []).append(tr)

    # H2 (TODO-0233, виток 4): reports of dead/rejected attempts used to
    # outlive the cycle in the outbox (live case: REVIEW-TODO-0006.md).
    # They belong to the archived unit — move them into done/<id>/ under
    # their own names; the BLOCKED .ready markers are signals, consumed
    # below like DONE/PROGRESS .ready.
    for prefix in ("BLOCKED", "REVIEW"):
        for tid_variant in (todo_id, _short_id(todo_id)):
            for ext in (".md", ".json"):
                src = outbox_p / f"{prefix}-{tid_variant}{ext}"
                if src.is_file():
                    moves.setdefault(dest / src.name, []).append(src)

    # Handoff files -> done/{id}/handoff/
    handoff_p = handoff_dir(project_dir)
    if handoff_p.is_dir():
        # Day-3 (dashboard review): also catch the legacy naming agents used —
        # '{role}-{todo}-final.md'. Two exact globs on purpose: a single
        # '*-{todo}*.md' would swallow TODO-00010 when archiving TODO-0001.
        candidates = set(handoff_p.glob(f"*-{todo_id}.md")) | set(
            handoff_p.glob(f"*-{todo_id}-*.md")
        )
        for hf in sorted(candidates):
            moves.setdefault(dest / "handoff" / hf.name, []).append(hf)

    # M3b (TODO-0233, architect-review): every DESTINATION LEAF, not only
    # ``dest``. The barrier above checks ``done/<id>`` as a directory; a
    # symlinked directory AT a leaf name (``done/<id>/DONE.md`` or the H2
    # ``BLOCKED-/REVIEW-<id>.md``) makes ``shutil.move`` write INTO it —
    # outside the project, leaving the link in place. Same F4 lesson as the
    # hygiene movers and ``records/<kind>``: the leaf is a destination too.
    # Runs after the read-only ``moves`` collection and before the first
    # mkdir/move, so a refusal has no side effect.
    try:
        for dst in moves:
            managed_fs.assert_no_symlink_escape(dst, root=root)
    except managed_fs.ManagedPathError as e:
        raise OSError(f"archive {todo_id} refused: {e}") from e

    moved_anything = False

    # Versioning (REVIEW F1) never refuses, so there is no validation
    # pass — the dest dir exists before the first suffix is chosen.
    dest.mkdir(parents=True, exist_ok=True)

    # Delete consumed signal files from inbox.
    # NOTE: {todo_id} already contains "TODO-" prefix (e.g. "TODO-0001"),
    # so dispatch signal is {todo_id}.ready (not TODO-{todo_id}.ready).
    for pattern in [f"{todo_id}.ready", f"ACK-{todo_id}.ready", f"APPROVE-{todo_id}.ready"]:
        p = inbox_p / pattern
        if p.exists():
            p.unlink()
            moved_anything = True

    # .ready signals from outbox are consumed, not archived.
    for prefix in ("PROGRESS", "DONE", "BLOCKED"):
        for tid_variant in (todo_id, _short_id(todo_id)):
            for name in (f"{prefix}-{tid_variant}.ready", f"{prefix}-{tid_variant}.md.ready"):
                src = outbox_p / name
                if src.is_file():
                    src.unlink()
                    moved_anything = True

    # A-12 + REVIEW F1: a done/<id>/ file is never overwritten.
    for dst, srcs in moves.items():
        if dst.is_file() and all(
            s.read_bytes() == dst.read_bytes() for s in srcs
        ):
            # Identical bytes — the same unit re-archived (idempotent):
            # keep the archive copy, consume the sources, no suffixes.
            for s in srcs:
                s.unlink()
            moved_anything = True
            continue
        # Different content (or a free name): the first source takes the
        # canonical name; every displaced file moves under a unique
        # suffix — the existing archive copy and any surplus source that
        # differs from the winner. Byte-identical surplus sources are
        # consumed (old dedupe). Both reports survive.
        dst_dir = dst.parent
        dst_dir.mkdir(parents=True, exist_ok=True)
        winner = srcs[0]
        displaced: list[Path] = []
        if dst.is_file():
            displaced.append(dst)
        for extra in srcs[1:]:
            if extra.read_bytes() == winner.read_bytes():
                extra.unlink()
            else:
                displaced.append(extra)
        # REVIEW F2: dst.name is reserved for the winner — a displaced
        # file must never receive the base name (with a free slot and
        # two different sources the first one would, and the winner
        # would then silently overwrite it — history lost, QA P1).
        for old in displaced:
            new_name = _suffixed_name(dst_dir, dst.name)
            shutil.move(str(old), str(dst_dir / new_name))
        shutil.move(str(winner), str(dst))
        moved_anything = True

    if not moved_anything:
        # AUD01-01: delete dest only if EMPTY. rmtree (regression badf05e)
        # wiped a previously assembled archive on a no-op re-call. rmdir
        # fails on non-empty — that failure is the safety guard.
        try:
            dest.rmdir()
        except OSError:
            pass
        return None

    return dest


def is_archived(project_dir: str | Path, todo_id: str) -> bool:
    """DF6-3: Check if a TODO has been archived to done/{todo_id}/."""
    from . import paths

    return (paths.done_dir(project_dir) / todo_id).is_dir()
