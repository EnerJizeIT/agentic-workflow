"""TODO state hygiene — stale-closure clearing and never-started removal.

RUN3 #4/#5: after an interrupted run a stale ``outbox/BLOCKED-<id>.ready``
keeps ``todos.is_closed`` true, so a re-issued TODO is invisible to
``awf_status`` / ``awf_start``. These helpers make the two repairs explicit
operations that leave a trace:

- :func:`unblock_todo` moves BLOCKED/ACK closure signals (canonical and
  legacy forms) into a timestamped ``context/`` directory. DONE signals are
  never touched — an archived TODO comes back only via ``awf restore``.
- :func:`remove_todo` deletes a TODO that never started (no PROGRESS, no
  signals; the dispatch ``.ready`` is an artifact and moves to the trace
  dir); ``context/BASELINE-<id>.*`` is deleted with the unit.
- :func:`retire_todo` archives a rejected/abandoned ACTIVE TODO (battle
  case TODO-0035: reject leaves ``DONE-<id>.{md,json}`` without the
  ``.ready`` signal, so the TODO stays "active" forever) — the files move
  to ``done/<id>/`` with a ``RETIRED-<ts>.md`` note; no DONE signal is
  written, and ``awf restore`` still works.
- :func:`update_todo` rewords a not-started TODO in place, keeping the
  number, ``.ready`` and the baseline (previous content backed up to
  ``context/TODO-<id>.md.bak-<ts>``); started TODOs are refused.
- :func:`clear_stale_closures` is the shared helper — ``dispatch_todo``
  calls it automatically on re-dispatch of the same number.
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .. import git_utils, paths, todos
from .._atomic import atomic_write_text
from .._log import log as _log
from .._names import unique_name
from ..pipeline_state import read_state
from ..signals import short_id
from ._background import check_pipeline_running
from ._errors import AwfApiError
from ._helpers import require_agentic
from ._results import RemoveTodoResult, RetireTodoResult, UnblockResult, UpdateTodoResult

_ID_RE = re.compile(r"^TODO-\d{4,}$")


def _validate_id(todo_id: str) -> None:
    if not _ID_RE.match(todo_id or ""):
        raise AwfApiError(f"invalid todo_id {todo_id!r}, expected TODO-NNNN")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def closure_files(project_dir: Path, todo_id: str) -> list[Path]:
    """All BLOCKED/ACK closure signal files for ``todo_id`` (canonical + legacy).

    Mirrors ``todos.is_closed`` file set (plus the ``.md`` companions and
    the BD-21 ``.md.ready`` typo form the engine also accepts). DONE-* is
    intentionally excluded: a DONE closure is lifted only by ``awf restore``.
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    short = short_id(todo_id)
    candidates = [
        outbox / f"BLOCKED-{todo_id}.ready",
        outbox / f"BLOCKED-{todo_id}.md",
        outbox / f"BLOCKED-{todo_id}.md.ready",
        inbox / f"ACK-{todo_id}.ready",
    ]
    if short != todo_id:
        candidates += [
            outbox / f"BLOCKED-{short}.ready",
            outbox / f"BLOCKED-{short}.md",
            outbox / f"BLOCKED-{short}.md.ready",
            inbox / f"ACK-{short}.ready",
        ]
    return [p for p in candidates if p.is_file()]


def has_done_closure(project_dir: Path, todo_id: str) -> bool:
    """True if a DONE-* closure signal (canonical or legacy) is in the outbox."""
    short = short_id(todo_id)
    outbox = paths.outbox(project_dir)
    for tid in dict.fromkeys((todo_id, short)):
        if (outbox / f"DONE-{tid}.ready").is_file():
            return True
    return False


def clear_stale_closures(project_dir: Path, todo_id: str) -> tuple[list[str], Path | None]:
    """Move all BLOCKED/ACK closures of ``todo_id`` into a trace dir.

    Destination: ``context/unblock-<todo_id>-<timestamp>/`` — each call gets
    its own directory, so repeated unblocks never overwrite an earlier trace.
    Returns ``(moved basenames, trace dir)`` — both empty/None when there is
    nothing to move. DONE-* signals are never moved.
    """
    files = closure_files(project_dir, todo_id)
    if not files:
        return [], None
    trace = paths.context_dir(project_dir) / f"unblock-{todo_id}-{_timestamp()}"
    trace.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for f in files:
        shutil.move(str(f), str(trace / f.name))
        moved.append(f.name)
    return moved, trace


def unblock_todo(project_dir: Path, todo_id: str) -> UnblockResult:
    """Clear stale BLOCKED/ACK closures so the TODO is active again.

    RUN3 #4: the manual repair for the TODO-0018 case — a stale
    ``BLOCKED-<id>.ready`` outlived a re-issue and hid the fresh TODO from
    ``awf_status``. Refused while the pipeline is running (moving a signal
    out from under a live engine would break its wait).
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    running, _pid, _tail = check_pipeline_running(project_dir)
    if running:
        raise AwfApiError(
            "pipeline is running — wait for verify/salvage, or awf kill first"
        )

    moved, trace = clear_stale_closures(project_dir, todo_id)
    if not moved:
        raise AwfApiError(
            f"No closure signals (BLOCKED/ACK) for {todo_id} — nothing to unblock. "
            "DONE closures are never lifted by unblock; use awf restore for an "
            "archived TODO."
        )

    trace_rel = trace.relative_to(project_dir).as_posix() if trace else ""
    _log(
        paths.logs_dir(project_dir),
        f"unblock: {todo_id} — cleared {', '.join(moved)} → {trace_rel}",
    )
    return UnblockResult(
        todo_id=todo_id,
        moved=moved,
        trace=trace_rel,
        message=(
            f"{todo_id} unblocked: {len(moved)} stale closure signal(s) "
            f"moved to {trace_rel}."
        ),
    )


def remove_todo(project_dir: Path, todo_id: str) -> RemoveTodoResult:
    """Delete a TODO that never started; the files keep a trace in done/.

    RUN3 #5: an inert TODO confuses ``awf status`` and blocks nothing —
    but it must not be erased silently either, so the file moves to
    ``done/<id>/removed-<timestamp>.md``.

    REPORTS26 B-f2 (TODO-0161): the dispatch ``.ready`` is an ARTIFACT of
    dispatch, not a sign of having started — it is no longer a refusal.
    The unit is removable when there is NO PROGRESS, NO outbound signal
    (DONE/BLOCKED/REVIEW and any ``*-<id>.*`` in the outbox) and NO
    inbound ACK/APPROVE. While removable, the ``.ready`` moves to the
    trace directory beside the renamed ``.md``, and
    ``context/BASELINE-<id>.*`` is DELETED (the unit no longer exists);
    the deleted files are reported in ``removed_files`` (project-relative
    paths). All other refusals are unchanged: a running pipeline, a
    missing ``.md``, a DONE closure (``awf restore``).
    """
    _validate_id(todo_id)
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    running, _pid, _tail = check_pipeline_running(project_dir)
    if running:
        raise AwfApiError(
            "pipeline is running — wait for verify/salvage, or awf kill first"
        )

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    md = inbox / f"{todo_id}.md"
    if not md.is_file():
        raise AwfApiError(f"{todo_id}.md not found in inbox — nothing to remove.")

    short = short_id(todo_id)
    ids = (todo_id, short) if short != todo_id else (todo_id,)
    outbox_signals = [
        p
        for tid in ids
        for p in sorted(outbox.glob(f"*-{tid}.*"))
        if p.is_file()
    ]
    inbox_signals = [
        p
        for tid in ids
        for prefix in ("ACK-", "APPROVE-")
        if (p := inbox / f"{prefix}{tid}.ready").is_file()
    ]
    if outbox_signals or inbox_signals:
        names = ", ".join(p.name for p in outbox_signals + inbox_signals)
        if any(p.name.startswith("DONE-") for p in outbox_signals):
            hint = "DONE — the TODO is closed as finished; use awf restore."
        else:
            hint = "stale BLOCKED/ACK — use awf unblock; orphans — awf reset --orphans."
        raise AwfApiError(
            f"{todo_id} has signals: {names} — it was started. {hint}"
        )
    if todos.has_progress(outbox, todo_id):
        raise AwfApiError(
            f"{todo_id} has PROGRESS — it was started. Use awf reset --orphans."
        )

    done_dir = paths.done_dir(project_dir) / todo_id
    done_dir.mkdir(parents=True, exist_ok=True)
    trace = done_dir / f"removed-{_timestamp()}.md"
    shutil.move(str(md), str(trace))
    trace_rel = trace.relative_to(project_dir).as_posix()

    # REPORTS26 B-f2: the dispatch .ready is an artifact — it follows the
    # .md into the trace directory instead of blocking the removal.
    ready = inbox / f"{todo_id}.ready"
    ready_moved = False
    if ready.is_file():
        # unique name: a re-dispatched number keeps its earlier trace copy
        ready_target = done_dir / _unique_name(done_dir, ready.name)
        shutil.move(str(ready), str(ready_target))
        ready_moved = True

    # The unit no longer exists — its baseline snapshot is dead weight
    # (commit gate / prove-red would read it for a TODO that is gone).
    removed_files: list[str] = []
    context_dir = paths.context_dir(project_dir)
    for tid in ids:
        for p in sorted(context_dir.glob(f"BASELINE-{tid}.*")):
            if p.is_file():
                removed_files.append(p.relative_to(project_dir).as_posix())
    removed_files.sort()
    for rel in removed_files:
        (project_dir / rel).unlink()

    note = f"todo-remove: {todo_id} — {md.name} → {trace_rel}"
    if ready_moved:
        note += f", {ready.name} → {done_dir.name}/"
    if removed_files:
        note += f", deleted {len(removed_files)} baseline file(s)"
    _log(paths.logs_dir(project_dir), note)

    message = f"{todo_id} removed (never started). Trace: {trace_rel}."
    if ready_moved:
        message += f" The dispatch .ready moved to done/{todo_id}/."
    if removed_files:
        message += f" Deleted {len(removed_files)} baseline file(s)."
    return RemoveTodoResult(
        todo_id=todo_id,
        trace_path=trace_rel,
        removed_files=removed_files,
        message=message,
    )


def _unique_name(dest_dir: Path, name: str) -> str:
    """A name that does not exist in ``dest_dir`` yet (never overwrite).

    ``PROGRESS-TODO-0001.md`` → ``PROGRESS-TODO-0001.md``, then
    ``PROGRESS-TODO-0001-1.md``, ``PROGRESS-TODO-0001-2.md``, ...
    (shared mechanism: awf/_names.py)
    """
    return unique_name(dest_dir, name)


def retire_todo(project_dir: Path, todo_id: str, reason: str) -> RetireTodoResult:
    """Archive a rejected/abandoned ACTIVE TODO — no fake DONE signal.

    RUN5 #2 (battle case TODO-0035): the reject path writes
    ``DONE-<id>.{md,json}`` / ``PROGRESS-<id>.md`` / ``REVIEW-<id>.md`` to
    the outbox but NOT ``DONE-<id>.ready`` — ``todos.is_closed`` stays
    false, so ``awf status`` / ``awf brief`` keep listing the TODO as
    active forever. Neither ``unblock`` (only BLOCKED/ACK), ``todo-remove``
    (refuses with ``.ready``) nor ``reset --orphans`` (it has PROGRESS)
    covers this state.

    Moves the TODO's files to ``done/<id>/`` — the same archive
    ``awf restore`` reads, so the recovery path survives:

    - ``inbox/TODO-<id>.md``      → ``done/<id>/TODO.md`` (restore shape)
    - ``inbox/TODO-<id>.ready``   → ``done/<id>/`` (kept, not deleted)
    - ``outbox/PROGRESS-<id>.*``  → ``done/<id>/``
    - ``outbox/DONE-<id>.{md,json}`` (NOT ``.ready``) → ``done/<id>/``
    - ``outbox/REVIEW-<id>.*``    → ``done/<id>/``

    (canonical and legacy short id forms). Writes
    ``done/<id>/RETIRED-<timestamp>.md`` with the reason, who (supervisor),
    time and the moved-file list, and logs to the orchestrator log.

    Refusals (clear errors, nothing moved):
    - no ``TODO-<id>.md`` in inbox and no ``done/<id>/TODO.md`` — not found;
    - ``done/<id>/TODO.md`` exists — already archived (use ``awf restore``);
    - a live pipeline on this id (PID alive + ``state/current.yaml``
      ``todo_id``) — ``awf kill`` or wait first;
    - empty ``reason`` — required (it becomes the RETIRED note body).
    """
    _validate_id(todo_id)
    reason = (reason or "").strip()
    if not reason:
        raise AwfApiError(
            "reason is required — it is written to the RETIRED note "
            "(why the TODO is retired)"
        )
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    md = inbox / f"{todo_id}.md"
    done_dir = paths.done_dir(project_dir) / todo_id
    archived_md = done_dir / "TODO.md"

    if archived_md.is_file():
        extra = (
            f" and a second copy sits in inbox/{md.name} — inspect manually"
            if md.is_file()
            else ""
        )
        raise AwfApiError(
            f"{todo_id} is already archived (done/{todo_id}/TODO.md{extra}) — "
            "use awf restore to bring an archived TODO back."
        )
    if not md.is_file():
        raise AwfApiError(
            f"{todo_id}.md not found in inbox and no done/{todo_id}/TODO.md — "
            "nothing to retire."
        )

    # Live pipeline on THIS id: the engine owns the files right now.
    # A pipeline on another id does not touch this TODO's files — allowed.
    running, pid, _tail = check_pipeline_running(project_dir)
    if running:
        state = read_state(project_dir)
        if isinstance(state, dict) and state.get("todo_id") == todo_id:
            raise AwfApiError(
                f"a live pipeline is running on {todo_id} (pid {pid}) — "
                "wait for it to finish or awf kill first"
            )

    short = short_id(todo_id)
    ids = (todo_id, short) if short != todo_id else (todo_id,)

    # (source, archive name) pairs — inbox first, then outbox artifacts.
    candidates: list[tuple[Path, str]] = [(md, "TODO.md")]
    ready = inbox / f"{todo_id}.ready"
    if ready.is_file():
        candidates.append((ready, ready.name))
    for tid in ids:
        candidates.extend(
            (p, p.name) for p in sorted(outbox.glob(f"PROGRESS-{tid}.*")) if p.is_file()
        )
        for ext in (".md", ".json"):
            p = outbox / f"DONE-{tid}{ext}"
            if p.is_file():
                candidates.append((p, p.name))
        candidates.extend(
            (p, p.name) for p in sorted(outbox.glob(f"REVIEW-{tid}.*")) if p.is_file()
        )

    done_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    pairs: list[tuple[str, str]] = []  # (from, to) — project-relative
    for src, name in candidates:
        target = done_dir / _unique_name(done_dir, name)
        shutil.move(str(src), str(target))
        to_rel = target.relative_to(project_dir).as_posix()
        moved.append(to_rel)
        pairs.append((src.relative_to(project_dir).as_posix(), to_rel))

    ts = _timestamp()
    note = done_dir / f"RETIRED-{ts}.md"
    note_lines = [
        f"# RETIRED: {todo_id}",
        "",
        f"- when: {ts}",
        "- by: supervisor (awf todo-retire)",
        f"- reason: {reason}",
        "",
        "## Moved to archive",
        "",
    ]
    note_lines += [f"- {src} → {to}" for src, to in pairs]
    note_lines.append("")
    note.write_text("\n".join(note_lines), encoding="utf-8")
    note_rel = note.relative_to(project_dir).as_posix()

    _log(
        paths.logs_dir(project_dir),
        f"retire: {todo_id} — {len(moved)} file(s) → "
        f"done/{todo_id}/ (note: RETIRED-{ts}.md, reason: {reason})",
    )
    return RetireTodoResult(
        todo_id=todo_id,
        moved=moved,
        retired_note=note_rel,
        message=(
            f"{todo_id} retired: {len(moved)} file(s) → done/{todo_id}/ "
            f"(note: RETIRED-{ts}.md)."
        ),
    )


def _recompute_include_baseline(
    project_dir: Path, todo_id: str, include_files: list[str]
) -> None:
    """REPORTS26 B-f2 (TODO-0161): recompute the unit's include metadata
    in place (not-started units only — the caller checked).

    ``BASELINE-<id>.untracked`` is rewritten from the CURRENT untracked
    state minus the included files — the same exclusion the dispatch
    applies (the unit never started, so everything untracked now is
    pre-existing). ``BASELINE-<id>.include`` is rewritten as the include
    trace, or CLEARED when the list is empty (the files return to the
    untracked snapshot). The rest of the baseline (``.sha``/``.status``/
    logs) is untouched — the sha still pins the dispatch moment.
    """
    from ..include_untracked import clear_include_audit, write_include_audit

    untracked = git_utils.git_stdout(
        project_dir, "ls-files", "--others", "--exclude-standard", check=False
    )
    excluded = set(include_files)
    if excluded:
        untracked = "\n".join(
            ln for ln in untracked.splitlines() if ln.strip() not in excluded
        )
    atomic_write_text(
        paths.context_dir(project_dir) / f"BASELINE-{todo_id}.untracked", untracked
    )
    if include_files:
        write_include_audit(project_dir, todo_id, include_files)
    else:
        clear_include_audit(project_dir, todo_id)


def _apply_append(text: str, block: str) -> str:
    """Append a block to the end of the TODO text, separated by one blank
    line (TODO-0162). The result always ends with a single newline."""
    return text.rstrip("\n") + "\n\n" + block.strip("\n") + "\n"


def _apply_section_update(text: str, heading: str, body: str) -> tuple[str, bool]:
    """Replace the body of the ``## <heading>`` section with ``body``
    (TODO-0162), up to the next level-2 ``## `` line or EOF. When the
    heading is absent the section is APPENDED at the end (replace-or-
    append) and the second return value is True.

    The output is a deterministic function of (text, heading, body), so
    a repeated identical update is idempotent: the file comes out
    byte-identical. When the heading occurs several times, the FIRST
    occurrence is the one replaced.
    """
    key = heading.strip()
    body_core = body.strip("\n")
    pat = re.compile(r"^##\s+" + re.escape(key) + r"\s*$")
    if not text.strip():
        section = f"## {key}" + ("\n" + body_core if body_core else "")
        return section + "\n", True
    ends_nl = text.endswith("\n")
    lines = text.split("\n")
    if ends_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    h = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
    if h is None:
        section = f"## {key}" + (f"\n{body_core}" if body_core else "")
        return text.rstrip("\n") + "\n\n" + section + "\n", True
    n = next(
        (i for i in range(h + 1, len(lines)) if re.match(r"^##\s", lines[i])),
        len(lines),
    )
    mid = [lines[h]] + (body_core.split("\n") if body_core else [])
    if n < len(lines):
        mid.append("")
    out = lines[:h] + mid + lines[n:]
    if ends_nl:
        out.append("")
    return "\n".join(out), False


def update_todo(
    project_dir: Path,
    todo_id: str,
    content: str = "",
    reason: str = "",
    include_untracked: list[str] | None = None,
    append: str | None = None,
    section_updates: dict[str, str] | None = None,
) -> UpdateTodoResult:
    """Reword a not-started TODO, keeping the number (RUN6 #4).

    Replaces the content of ``inbox/TODO-<id>.md`` in place: the number,
    the dispatch ``.ready`` signal and the baseline snapshot stay
    untouched (the baseline pins a git sha, not the text). The previous
    content is backed up byte-identical to
    ``context/TODO-<id>.md.bak-<timestamp>`` (unique name, never
    overwritten) and the operation is logged to the orchestrator log
    (with ``reason``, when given).

    REPORTS26 B-f1 (TODO-0162): two partial-edit modes for a
    not-started TODO, each keeping the number/``.ready``/baseline and
    writing a backup like a full reword:
    - ``append`` — a block is added to the END of the md, separated by
      one blank line; the previous text is untouched;
    - ``section_updates`` — a mapping ``{heading: new body}``: the BODY
      of the ``## <heading>`` section (up to the next level-2 ``## ``
      line or EOF) is replaced; an ABSENT heading is added as a new
      section at the end (replace-or-append). A repeated identical call
      is idempotent — the file comes out byte-identical.

    Exactly ONE content mode per call: ``content`` OR ``append`` OR
    ``section_updates``; two or more at once is refused, as is an empty
    ``append`` or an empty ``section_updates`` map. ``include_untracked``
    (below) is orthogonal and stays combinable with any content mode.

    REPORTS26 B-f2 (TODO-0161): ``include_untracked`` — the unit's
    pre-existing-untracked inclusion for a NOT-STARTED unit. Same
    validation as the dispatch (each path must exist, be untracked, be
    not gitignored, stay inside the project; all-or-nothing — the FIRST
    invalid path refuses BEFORE any side effect). Accepted, it
    recomputes ``BASELINE-<id>.untracked`` (current untracked state
    minus the included files) and rewrites the
    ``BASELINE-<id>.include`` trace; an EMPTY list clears the inclusion
    (the files return to the untracked snapshot). An empty ``content``
    is allowed WHEN ``include_untracked`` is given (and no ``append`` /
    ``section_updates``) — the TODO text is not touched (no backup) and
    the answer carries ``backup=""``.

    Refusals (clear errors, nothing written):
    - no ``TODO-<id>.md`` in the inbox;
    - two or more content modes at once (``content``/``append``/
      ``section_updates``); an empty ``append``; an empty
      ``section_updates`` map; an empty heading key in
      ``section_updates``;
    - no content mode at all AND no ``include_untracked``;
    - an invalid ``include_untracked`` path (before any side effect);
    - the TODO already started — any outbox signal (``PROGRESS-*``,
      ``BLOCKED-*``, ``DONE-*``, ``REVIEW-*``, canonical or legacy), an
      inbox ``ACK-``/``APPROVE-`` closure, or a non-empty ``PROGRESS`` —
      the unit is in flight: fix it via REVIEW/replan, or retire it and
      re-dispatch;
    - a live pipeline on this id (the engine owns the file right now —
      a pipeline on another id does not touch this TODO's files).
    """
    _validate_id(todo_id)
    content = content or ""
    has_content = bool(content.strip())
    if append is not None and not append.strip():
        raise AwfApiError(
            "append is empty — nothing to append (omit append to keep the "
            "text as is)"
        )
    if section_updates is not None and not section_updates:
        raise AwfApiError(
            "section_updates is an empty map — nothing to update (omit "
            "section_updates to keep the text as is)"
        )
    if section_updates is not None and any(not k.strip() for k in section_updates):
        raise AwfApiError(
            "section_updates: an empty heading key — name the ## heading "
            "to replace or add"
        )
    modes = [
        name
        for name, on in (
            ("content", has_content),
            ("append", append is not None),
            ("section_updates", section_updates is not None),
        )
        if on
    ]
    if len(modes) > 1:
        raise AwfApiError(
            f"exactly one of content/append/section_updates per call — "
            f"got {', '.join(modes)}"
        )
    if not modes and include_untracked is None:
        raise AwfApiError(
            "content is empty and no include_untracked — nothing to update with"
        )
    sections = section_updates if section_updates is not None else {}
    project_dir = Path(project_dir).resolve()
    require_agentic(project_dir)

    md = paths.inbox(project_dir) / f"{todo_id}.md"
    if not md.is_file():
        raise AwfApiError(f"{todo_id}.md not found in inbox — nothing to update.")

    # Live pipeline on THIS id: the engine owns the files right now.
    # A pipeline on another id does not touch this TODO's files — allowed.
    running, pid, _tail = check_pipeline_running(project_dir)
    if running:
        state = read_state(project_dir)
        if isinstance(state, dict) and state.get("todo_id") == todo_id:
            raise AwfApiError(
                f"a live pipeline is running on {todo_id} (pid {pid}) — "
                "wait for it to finish or awf kill first"
            )

    outbox = paths.outbox(project_dir)
    short = short_id(todo_id)
    ids = (todo_id, short) if short != todo_id else (todo_id,)
    outbox_signals = [
        p
        for tid in ids
        for p in sorted(outbox.glob(f"*-{tid}.*"))
        if p.is_file()
    ]
    inbox_signals = [
        p
        for tid in ids
        for prefix in ("ACK-", "APPROVE-")
        if (p := paths.inbox(project_dir) / f"{prefix}{tid}.ready").is_file()
    ]
    if outbox_signals or inbox_signals or todos.has_progress(outbox, todo_id):
        names = ", ".join(p.name for p in outbox_signals + inbox_signals) or "PROGRESS"
        raise AwfApiError(
            f"{todo_id} has signals: {names} — it is in flight. Fix the unit "
            "via REVIEW/replan, or retire it (awf todo-retire) and "
            "re-dispatch."
        )

    # REPORTS26 B-f2: the inclusion is validated BEFORE any side effect —
    # the dispatch's all-or-nothing contract (no backup, no snapshot
    # rewrite on refusal). The baseline is recomputed before the content
    # write, so a failed write still leaves a consistent include state.
    include_files: list[str] | None = None
    if include_untracked is not None:
        from ..include_untracked import resolve_include_untracked

        include_files = (
            [] if not include_untracked
            else resolve_include_untracked(project_dir, include_untracked)
        )
        _recompute_include_baseline(project_dir, todo_id, include_files)

    has_any_content_mode = has_content or append is not None or section_updates is not None
    if not has_any_content_mode:
        if include_files:
            what = (
                f"include_untracked: {len(include_files)} pre-existing file(s) "
                "re-claimed into the unit commit"
            )
        else:
            what = (
                "include_untracked cleared (files returned to the untracked "
                "snapshot)"
            )
        _log(
            paths.logs_dir(project_dir),
            f"todo-update: {todo_id} — {what}; baseline recomputed, content kept",
        )
        return UpdateTodoResult(
            todo_id=todo_id,
            backup="",
            message=f"{todo_id} updated: {what}; number/.ready/baseline sha kept.",
        )

    old_content = md.read_text(encoding="utf-8")
    context_dir = paths.context_dir(project_dir)
    context_dir.mkdir(parents=True, exist_ok=True)
    ts = _timestamp()
    bak_name = _unique_name(context_dir, f"{todo_id}.md.bak-{ts}")
    backup = context_dir / bak_name
    backup.write_text(old_content, encoding="utf-8")
    backup_rel = backup.relative_to(project_dir).as_posix()

    if has_content:
        new_content = content
        what = f"content replaced ({len(content)} chars)"
    elif append is not None:
        new_content = _apply_append(old_content, append)
        what = f"appended ({len(append.strip())} chars)"
    else:
        new_content = old_content
        replaced: list[str] = []
        added: list[str] = []
        for heading, body in sections.items():
            new_content, is_added = _apply_section_update(new_content, heading, body)
            (added if is_added else replaced).append(heading.strip())
        parts = []
        if replaced:
            parts.append("replaced: " + ", ".join(replaced))
        if added:
            parts.append("added: " + ", ".join(added))
        what = "section_updates — " + "; ".join(parts)

    md.write_text(new_content, encoding="utf-8")

    why = f", reason: {reason.strip()}" if reason.strip() else ""
    include_note = ""
    if include_files is not None:
        include_note = (
            f", include_untracked: {len(include_files)} file(s)"
            if include_files
            else ", include_untracked: cleared"
        )
    _log(
        paths.logs_dir(project_dir),
        f"todo-update: {todo_id} — {what}, "
        f"backup: {backup_rel}{include_note}{why})",
    )
    message = (
        f"{todo_id} updated: {what}, number/.ready/baseline "
        f"kept (backup: {backup_rel})."
    )
    if include_files is not None:
        message += (
            f" include_untracked: {len(include_files)} file(s)."
            if include_files
            else " include_untracked: cleared."
        )
    return UpdateTodoResult(
        todo_id=todo_id,
        backup=backup_rel,
        message=message,
    )


__all__ = [
    "closure_files",
    "clear_stale_closures",
    "has_done_closure",
    "remove_todo",
    "retire_todo",
    "unblock_todo",
    "update_todo",
]
