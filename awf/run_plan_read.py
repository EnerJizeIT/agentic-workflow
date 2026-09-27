"""ORCH M1.2: the single reader of the run (забег) record.

``awf_brief`` (the compact card) and ``awf_load_supervisor_context`` (the
full context) are two views of ONE record, not parallel retellings
(IMPLEMENTATION-STRATEGY §1.2): both call :func:`read_run_record` and
render different amounts of it — the card shows goal, position, budget,
the last decision in one line and links to the sources; the full context
adds all the decisions (which, why, when) and the vision/plan excerpts.

The record is DERIVED from ``.agentic/state/run.yaml`` (the single
source, ORCH M1.1) — there is no second store. A corrupted file (broken
YAML, invalid shape, non-UTF-8) degrades to "no run" with a ``warning``:
the surfaces keep working, the file is kept for inspection
(``run_state.read_run`` contract, AUD02-06). A corrupt FIELD in an
otherwise valid file (a non-numeric ``budget_minutes``) degrades that
field to a safe default with a ``warning`` — the same M1.1 style, no
traceback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import paths, run_state

# Card line budgets (the brief card has a 900-word limit and little
# headroom): the goal and the decision reason are clipped to fit ONE
# compact line each; the full context carries the unclipped text.
_GOAL_MAX_WORDS = 10
_REASON_MAX_WORDS = 10


@dataclass
class RunRecord:
    """One reading of the run state, shared by every supervisor surface."""

    active: bool = False
    goal: str = ""
    criteria: list[str] = field(default_factory=list)
    position: str = "0/0"
    current: str = ""
    budget_minutes: int = 0
    budget_left_minutes: int = 0
    note: str = ""
    # the causal memory: [{ts, kind, todo_id, reason}] (sanitized by read_run)
    decisions: list[dict[str, str]] = field(default_factory=list)
    last_decision: dict[str, str] | None = None
    # project-relative files that back the record: run.yaml + the
    # REVIEW/RUN-EVIDENCE file of the last decision + the RUN-REPORT of a
    # closed run
    sources: list[str] = field(default_factory=list)
    # non-empty when a corrupted run.yaml degraded the record
    warning: str = ""
    # the sanitized raw state (None = absent or unreadable) — consumers
    # that need extra fields (run_brief: queue, stop_reason, no_checkpoints)
    # read them from here instead of re-parsing the file
    state: dict | None = None


def _clip_words(text: str, max_words: int) -> str:
    """Clip to max_words with an ellipsis (one-line card fields)."""
    text = str(text or "").strip()
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " …"


def _sources(project_dir: Path, record: RunRecord) -> list[str]:
    """The files that back the record (project-relative, one link each)."""
    sources = [".agentic/state/run.yaml"]
    d = record.last_decision
    if d:
        todo = str(d.get("todo_id") or "")
        kind = str(d.get("kind") or "")
        if todo:
            if kind == "reject":
                p = paths.outbox(project_dir) / f"REVIEW-{todo}.md"
                if p.is_file():
                    sources.append(f"outbox/REVIEW-{todo}.md")
            elif kind == "approve":
                p = paths.context_dir(project_dir) / f"RUN-EVIDENCE-{todo}.md"
                if p.is_file():
                    sources.append(f"context/RUN-EVIDENCE-{todo}.md")
    report = str((record.state or {}).get("report_file") or "")
    if report:
        try:
            rel = Path(report).resolve().relative_to(Path(project_dir).resolve())
            if rel.is_file():
                sources.append(rel.as_posix())
        except (ValueError, OSError):
            pass
    return sources


def read_run_record(project_dir: Path) -> RunRecord:
    """Read the run state ONCE for a supervisor surface.

    Returns an empty record when no run was ever started; a record with a
    ``warning`` when run.yaml exists but cannot be read (the file is kept
    for inspection, both surfaces degrade to "no run").
    Never raises on file state — the surfaces are read-only info tools.
    """
    try:
        project_dir = Path(project_dir).expanduser().resolve()
        f = run_state.run_file(project_dir)
        exists = f.is_file()
        state = run_state.read_run(project_dir)
    except OSError:
        return RunRecord()
    if state is None:
        record = RunRecord()
        if exists:
            record.warning = (
                "run.yaml is unreadable (broken YAML or invalid shape) — "
                "treated as no run; the file is kept at "
                ".agentic/state/run.yaml"
            )
        return record

    record = RunRecord(active=bool(state.get("active")), state=state)
    record.goal = str(state.get("goal") or "")
    record.criteria = [str(c) for c in (state.get("criteria") or [])]
    record.position = run_state.position(state)
    record.current = str(state.get("current") or "")
    record.note = str(state.get("note") or "")
    record.decisions = [dict(d) for d in (state.get("decisions") or [])]
    if record.decisions:
        record.last_decision = record.decisions[-1]
    try:
        budget = int(state.get("budget_minutes", 0) or 0)
    except (TypeError, ValueError):
        # Invariant 5: a corrupt budget_minutes in an otherwise valid-shape
        # run.yaml (hand-edited; the write path cannot produce it) degrades
        # to 0 with a warning — M1.1 style, no traceback, file kept.
        budget = 0
        record.warning = (
            "run.yaml 'budget_minutes' is not a number — treated as 0 "
            "(corrupt file kept for inspection)"
        )
    record.budget_minutes = budget
    if budget:
        # B2: the budget is in PRODUCTIVE minutes (same formula as run_brief)
        left = budget - run_state.productive_minutes(state)
        record.budget_left_minutes = max(0, int(left))
    record.sources = _sources(project_dir, record)
    return record


def clip_goal(goal: str, max_words: int = _GOAL_MAX_WORDS) -> str:
    """The card's one-line goal (the full context keeps the unclipped text)."""
    return _clip_words(goal, max_words)


def decision_line(d: dict[str, str] | None) -> str:
    """One line for a decision: ``reject TODO-0001 — reason`` (reason clipped)."""
    if not d:
        return ""
    line = f"{str(d.get('kind') or '?')} {str(d.get('todo_id') or '?')}"
    reason = str(d.get("reason") or "").strip()
    if reason:
        line += f" — {_clip_words(reason, _REASON_MAX_WORDS)}"
    return line


def next_action_for_record(record: RunRecord, fallback: str) -> str:
    """The nearest permitted action, run-aware.

    Outside a run (or before the first decision) the fallback stands —
    the behavior is unchanged. Inside an active run after a REJECT the
    fallback is replaced: the decision is recorded, and the next step is
    to fix the assignment and ``awf_run_next`` (in a run the queue
    position is owned by awf — not the generic ``awf_start``).
    """
    if not record.active or not record.last_decision:
        return fallback
    if str(record.last_decision.get("kind") or "") != "reject":
        return fallback
    todo = str(record.last_decision.get("todo_id") or "?")
    return (
        f"Decision recorded: reject {todo} (sources in the run record). "
        f"Next permitted step: fix the assignment, then `awf_run_next` "
        f"(the run gate counts rejections — twice stops the run). Stop the "
        f"run instead with `awf_run_finish` if the task needs the owner."
    )


__all__ = [
    "RunRecord",
    "read_run_record",
    "clip_goal",
    "decision_line",
    "next_action_for_record",
]
