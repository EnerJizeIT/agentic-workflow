"""A32/A34: the refusal's own decisions — stay in the unit, bounded replan.

One module for the pure parts of a refusal: whether a policy may grow the
queue (``rejection_stays_in_unit``), which stage owns the finding
(``reject_fix_target``), what the replan prompt may carry and how big it
may get, and the evidence writers around those facts.

The engine keeps the transition handlers (``_handle_rejected`` /
``_maybe_rewind_rejection``) and ``_handle_fix_round`` with its rollback
budget: those are the A7 round machinery and moving them would split one
contract across two modules. Everything here is a call the handlers make.

Why a separate file at all: ``pipeline_engine`` and ``supervisor`` are
already past the 400-line ratchet, and this unit's new logic has one owner
(the refusal), not two homes (the engine loop and the supervisor stage).
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from ._log import log as _log
from .pipeline import Stage
from .signals import find_signal_file, iter_review_reports


def rejection_stays_in_unit(stage: Stage) -> bool:
    """A32: a rejection rewinds the SAME unit unless the policy says replan.

    ``replan`` is the only policy value that sanctions a new unit — and it
    is the only one whose path spawns the replan subprocess.
    """
    return (stage.on_rejected or "escalate") != "replan"


def reject_fix_target(stages: list[Stage], stage_idx: int) -> str:
    """A32: the stage a rejection rewinds to when the policy names none.

    The nearest earlier stage that runs a role (``kind == "execute"``) —
    the author the reviewer's finding belongs to. A rejection on the first
    stage has no earlier author, so the caller falls back to escalation.
    """
    for idx in range(stage_idx - 1, -1, -1):
        if stages[idx].kind == "execute":
            return stages[idx].name
    return ""


def is_retired_unit(project_dir: Path, todo_id: str) -> bool:
    """A32/A34: True when ``done/<id>/`` carries a RETIRED note.

    A retired unit was taken out of the queue on purpose — its id must not
    come back as a "new" clone in a replan answer.
    """
    if not todo_id:
        return False
    done = paths.done_dir(project_dir) / todo_id
    try:
        return any(done.glob("RETIRED-*.md"))
    except OSError:
        return False


def append_progress_note(project_dir: Path, todo_id: str, note: str) -> None:
    """A34: one line into ``PROGRESS-<id>.md`` (evidence, not a task).

    The progress parser counts only ``## Task`` lines, so a plain note
    never changes the unit's task counters.
    """
    from datetime import datetime, timezone

    if not todo_id:
        return
    progress = paths.outbox(project_dir) / f"PROGRESS-{todo_id}.md"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with progress.open("a", encoding="utf-8") as handle:
            handle.write(f"\n## replan — {stamp}\n\n{note}\n")
    except OSError:
        pass


def replan_budget_chars() -> int:
    """A32/A34: the engine's existing cap for text handed to a role.

    ``pipeline_engine.FIX_ROUND_MAX_CHARS`` is that cap already — the replan
    prompt reuses it instead of inventing a second limit. Imported lazily:
    pipeline_engine imports this module.
    """
    from .pipeline_engine import FIX_ROUND_MAX_CHARS

    return int(FIX_ROUND_MAX_CHARS)


def _read_text_block(path: Path) -> str:
    """File body or "" — a missing/unreadable block never breaks the prompt."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _newest_stage_report(handoff_dir: Path, todo_id: str) -> Path | None:
    """The ONE report of the stage that slid — never the handoff history."""
    try:
        reports = [p for p in handoff_dir.glob(f"*-{todo_id}.md") if p.is_file()]
        if not reports:
            return None
        return max(reports, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None


def build_replan_context(
    todo_id: str,
    project_dir: Path,
    logs_dir: Path | None = None,
    budget: int | None = None,
) -> tuple[str, list[str]]:
    """A32/A34: the replan prompt — reviewer report, TODO, stage report.

    What goes in: the reviewer's report (``REVIEW-<id>.md`` or the worker's
    ``REVIEW-REJECTED-<id>.md`` — the two spellings live in ``signals``),
    the current TODO, the report of the stage that slid (the newest handoff
    for this unit) and the BLOCKED note. What never goes in: the handoff
    history, ``DONE-*.md`` archives, other units' files.

    The whole assembled text — opening line, body and closing instruction —
    is measured against the role budget; the closing instruction is part of
    this builder so no caller can push the result over the limit after the
    check. Cut order, fixed: the stage report, the BLOCKED note, then the
    TODO is truncated, then the reviewer report. Every cut is logged.
    Returns (prompt, dropped labels).
    """
    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)
    dropped: list[str] = []

    review_text = ""
    # One enumeration (``signals``), this builder's own rule on top: the
    # first non-empty report wins, so an empty decision file never masks
    # the other spelling.
    for candidate in iter_review_reports(outbox, todo_id):
        review_text = _read_text_block(candidate)
        if review_text:
            break
    todo_text = _read_text_block(inbox / f"{todo_id}.md")
    blocked_path = find_signal_file(outbox, "BLOCKED", todo_id, ".md")
    blocked_text = _read_text_block(blocked_path) if blocked_path is not None else ""
    stage_path = _newest_stage_report(paths.handoff_dir(project_dir), todo_id)
    stage_text = _read_text_block(stage_path) if stage_path is not None else ""

    # Head and tail state ONE contract: this path runs only under
    # ``on_rejected: replan``, and the engine's wait accepts only a NEW
    # TODO carrying ``replan_for``. A head that said "refine the SAME unit"
    # sent the supervisor to append, the wait never saw an answer, and the
    # run stopped ("Supervisor did not answer the escalation").
    head = (
        f"Supervisor replan for {todo_id}. A stage refused or reported BLOCKED: "
        "read the reviewer report and the TODO, then write ONE refined TODO "
        "for the next step of the same work — not a copy of the whole unit.\n"
    )
    tail = (
        f"Replan policy only: write a new TODO with ``replan_for: {todo_id}`` "
        "in its contract block, baseline it, and create its ``.ready`` signal.\n"
    )
    budget_value = replan_budget_chars() if budget is None else int(budget)

    sections: list[tuple[str, str]] = []
    if review_text:
        sections.append(("reviewer report", review_text))
    if todo_text:
        sections.append(("current TODO", todo_text))

    def render(items: list[tuple[str, str]]) -> str:
        body = "\n\n".join(f"## {label}\n\n{text}" for label, text in items)
        return f"{head}\n{body}\n{tail}"

    kept = list(sections)
    for label, text in (("stage report", stage_text), ("BLOCKED note", blocked_text)):
        if not text:
            continue
        if len(render([*kept, (label, text)])) <= budget_value:
            kept.append((label, text))
        else:
            dropped.append(label)

    # Last resort: the TODO gives way first, then the reviewer's report.
    # By LABEL, not by position: when one of the two is missing the other is
    # the only section left, and a positional walk used to skip it entirely —
    # the overflow was then taken off the END of the prompt, cutting the
    # closing ``replan_for`` instruction the supervisor's answer is keyed on.
    prompt = render(kept)
    if len(prompt) > budget_value:
        overflow = len(prompt) - budget_value
        for wanted in ("current TODO", "reviewer report"):
            if overflow <= 0:
                break
            idx = next(
                (i for i, (label, _text) in enumerate(kept) if label == wanted),
                None,
            )
            if idx is None:
                continue
            label, text = kept[idx]
            keep = max(0, len(text) - overflow)
            if keep < len(text):
                dropped.append(f"{label} truncated")
                overflow -= len(text) - keep
            kept[idx] = (label, text[:keep])
        prompt = render(kept)
    if len(prompt) > budget_value:
        # The fixed instruction alone is over the budget: a limit too small
        # to carry the replan at all. The head survives (it names the unit
        # and the rule) and the fact is recorded like any other cut.
        dropped.append("fixed instructions truncated")
        prompt = prompt[:budget_value]

    if dropped and logs_dir is not None:
        _log(
            logs_dir,
            f"A32/A34: replan prompt over {budget_value} chars — cut: "
            + ", ".join(dropped),
        )
    return prompt, dropped


__all__ = [
    "append_progress_note",
    "build_replan_context",
    "is_retired_unit",
    "reject_fix_target",
    "rejection_stays_in_unit",
    "replan_budget_chars",
]
