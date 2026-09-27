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
# ORCH M2.3: the card's evidence plan stays brief — one verify command
# per line, clipped to ONE line, at most three commands (the context
# carries all of them unclipped).
_VERIFY_MAX_WORDS = 12
_PLAN_MAX_ITEMS = 3


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
    # ORCH M2.3: the per-item evidence plan of the CURRENT element (the
    # last recorded one when current has none) — the launch-time snapshot
    # of the TODO's contract (verify/gates/prove_red) + file hash. None
    # when nothing was launched (or the legacy state carries no plans).
    evidence_plan: dict | None = None
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


def _evidence_plan_for(state: dict) -> dict | None:
    """ORCH M2.3: which evidence plan a surface shows — the CURRENT
    element's snapshot when one was recorded for it, else the LAST
    recorded entry (a closed run keeps its last element's plan; a re-
    launched item is matched by id, not by position). None when the
    (sanitized) state carries no plans at all."""
    plans = state.get("evidence_plans")
    if not isinstance(plans, list) or not plans:
        return None
    current = str(state.get("current") or "")
    if current:
        for e in reversed(plans):
            if isinstance(e, dict) and str(e.get("todo_id") or "") == current:
                return dict(e)
    last = plans[-1]
    return dict(last) if isinstance(last, dict) else None


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
    # ORCH M2.3: the current/last element's evidence plan (the sanitized
    # state — a broken field already degraded to [] in read_run).
    record.evidence_plan = _evidence_plan_for(state)
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


def evidence_plan_lines(record: RunRecord, *, compact: bool = False) -> list[str]:
    """ORCH M2.3: the evidence plan lines for a supervisor surface — the
    verify commands and the prove_red ids, brief, before the decision.

    ``compact=True`` (the brief card) keeps the word budget: at most
    ``_PLAN_MAX_ITEMS`` commands/ids, each clipped to one line; the full
    context (``compact=False``) carries every item unclipped. No plan →
    [] (nothing launched yet); a plan with an empty contract renders the
    note (the TODO had no contract block / the block was broken).
    """
    plan = record.evidence_plan
    if not plan:
        return []
    todo = str(plan.get("todo_id") or "?")
    sha = str(plan.get("todo_sha") or "")
    head = f"- run evidence plan ({todo}"
    if sha:
        head += f", file sha {sha[:12]}…"
    lines = [head + "):"]
    verify = [str(v).strip() for v in (plan.get("verify") or []) if str(v).strip()]
    gates = [str(g).strip() for g in (plan.get("gates") or []) if str(g).strip()]
    prove = [str(p).strip() for p in (plan.get("prove_red") or []) if str(p).strip()]
    if verify:
        shown = verify if not compact else verify[:_PLAN_MAX_ITEMS]
        for v in shown:
            lines.append(f"  - verify: {_clip_words(v, _VERIFY_MAX_WORDS) if compact else v}")
        if compact and len(verify) > _PLAN_MAX_ITEMS:
            lines.append(f"  - … +{len(verify) - _PLAN_MAX_ITEMS} more verify command(s)")
    if gates:
        lines.append(f"  - gates: {', '.join(gates)}")
    if prove:
        shown = prove if not compact else prove[:_PLAN_MAX_ITEMS]
        for p in shown:
            lines.append(f"  - prove_red: {p}")
        if compact and len(prove) > _PLAN_MAX_ITEMS:
            lines.append(f"  - … +{len(prove) - _PLAN_MAX_ITEMS} more prove_red id(s)")
    note = str(plan.get("note") or "").strip()
    if note:
        lines.append(f"  - note: {note}")
    return lines


def next_action_for_record(record: RunRecord, fallback: str) -> str:
    """The nearest permitted action, run-aware.

    Outside a run (or before the first decision) the fallback stands —
    the behavior is unchanged. Inside an active run after a REJECT the
    fallback is replaced: the decision is recorded, and the ONE next
    step is to CLOSE it — re-plan the task (issue a new TODO) and retire
    the rejected one, or stop the run (ORCH M2.3: the old wording told
    the supervisor "fix the assignment, then awf_run_next", but the run
    gate REFUSES that run_next until the rejected TODO is archived —
    ``awf/api/run.py`` refuses over an open reject; the retire step was
    missing from the text). ``awf_run_next`` stays named as the loop
    step AFTER the closing (the queue position is owned by awf — not
    the generic ``awf_start``).
    """
    if not record.active or not record.last_decision:
        return fallback
    if str(record.last_decision.get("kind") or "") != "reject":
        return fallback
    todo = str(record.last_decision.get("todo_id") or "?")
    return (
        f"Decision recorded: reject {todo} (sources in the run record). "
        f"Close the reject decision: re-plan the task (issue a new TODO) "
        f"and retire {todo} (`awf_todo_retire`), or stop the run "
        f"(`awf_run_finish`) if it needs the owner. Then `awf_run_next` "
        f"launches the next item (the run gate counts rejections — twice "
        f"stops the run)."
    )


__all__ = [
    "RunRecord",
    "read_run_record",
    "clip_goal",
    "decision_line",
    "evidence_plan_lines",
    "next_action_for_record",
]
