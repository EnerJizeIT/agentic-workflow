"""Autonomous run (забег) API — SPEC A-run v1.

The supervisor chat drives the loop; awf keeps the state and enforces the
mechanical gates:

    run_start → (write TODO) → run_next → [pipeline runs] → verify event
    → supervisor probes → approve(evidence=...) / reject → run_next → ...

Stop-list gates (enforced here, not by the supervisor's conscience):
queue exhausted, budget exceeded, stop flags on the next TODO, the TODO was
rejected twice, or the previous TODO is not finished.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from .. import git_utils, paths, pipeline_state, run_state, todos
from .._atomic import atomic_write_text
from ..todo_ids import is_valid_todo_id
from . import _liveness
from ._errors import AwfApiError
from ._helpers import require_awf_project
from ._results import (
    RunFinishResult,
    RunNextResult,
    RunReviseResult,
    RunStartResult,
    RunStatusResult,
    ServiceRunApproveResult,
    ServiceRunResult,
    ServiceRunStatusResult,
)


def _require_run_project(project_dir: Path) -> Path:
    """run_* safety: a real project root has .agentic/ AND config.yaml.

    NEG-2026-09-19 A1: ``run_start`` without ``project_dir`` used the MCP
    subprocess cwd ($HOME), where a bare ``.agentic/`` from old experiments
    existed — a ghost run was recorded there and every later message was
    misleading. Refuse loudly, with the path, before writing anything.

    REPORTS26 B2 (TODO-0156): the strict check now lives in
    :func:`require_awf_project` — one home for all write entry points.
    """
    return require_awf_project(project_dir)


def _validate_queue(queue: list[str | dict] | None) -> list[dict]:
    """RUN3 #2: validate + normalize the queue.

    Items may be ``TODO-NNNN`` strings (legacy, pipeline from config) or
    objects ``{"todo_id": "TODO-NNNN", "pipeline": "<name>"}`` — mixed lists
    are fine. Returns the canonical form ``[{"todo_id", "pipeline"}, ...]``
    (empty ``pipeline`` = the config default).
    """
    items: list[dict] = []
    for q in queue or []:
        if isinstance(q, dict):
            todo_id = str(q.get("todo_id", "")).strip()
            pipeline = str(q.get("pipeline", "") or "").strip()
            if not is_valid_todo_id(todo_id):
                raise AwfApiError(
                    f"invalid queue item {q!r} — an object item needs "
                    "{'todo_id': 'TODO-NNNN', 'pipeline': '<name>'} "
                    "(pipeline may be empty for the config default)"
                )
            items.append({"todo_id": todo_id, "pipeline": pipeline})
        else:
            s = str(q).strip()
            if not s:
                continue
            if not is_valid_todo_id(s):
                raise AwfApiError(f"invalid TODO id {s!r} in queue — expected TODO-NNNN")
            items.append({"todo_id": s, "pipeline": ""})
    if not items:
        raise AwfApiError(
            "queue is required — a non-empty list of TODO ids or "
            "{'todo_id', 'pipeline'} objects, e.g. ['TODO-0010', "
            "{'todo_id': 'TODO-0011', 'pipeline': 'audit-llm'}]"
        )
    ids = [i["todo_id"] for i in items]
    if len(ids) != len(set(ids)):
        raise AwfApiError("queue contains duplicate TODO ids")
    return items


def _queue_item_label(item: object) -> str:
    """Human label for a queue item: the id, plus the pipeline when set."""
    if isinstance(item, dict):
        tid = str(item.get("todo_id", ""))
        pipe = str(item.get("pipeline", "") or "")
        return f"{tid} ({pipe})" if pipe else tid
    return str(item)


def _current_pipeline_hint(queue_items: object, current_id: str) -> str:
    """RUN3 #2: the running item's pipeline, when pinned explicitly in the
    queue ("" = config default — nothing to show, do not bloat the line)."""
    if not current_id:
        return ""
    for item in queue_items or []:
        if isinstance(item, dict) and item.get("todo_id") == current_id:
            pipe = str(item.get("pipeline", "") or "")
            return f" [pipeline: {pipe}]" if pipe else ""
    return ""


def _todo_declared_pipeline(todo_md: Path) -> str:
    """RUN3 #2 (Part B): the pipeline declared in the TODO's contract block.

    Read through the shared contract parser so the block keeps ONE meaning.
    Absent block / absent key / unreadable file → ``""`` (config default).
    A broken block also degrades to ``""`` here — the engine's own contract
    handling surfaces it at verify time; run_next must not crash on it.
    """
    from ..unit_contract import parse_todo_contract

    try:
        content = todo_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        contract, _unknown = parse_todo_contract(content)
    except ValueError:
        return ""
    if not contract:
        return ""
    return str(contract.get("pipeline", "") or "").strip()


def _todo_evidence_plan(todo_md: Path) -> dict:
    """ORCH M2.3: the launch-time snapshot of a TODO's evidence plan.

    Returns ``{todo_sha, verify, gates, prove_red, note}`` — the file hash
    (sha256 of the TODO's bytes) plus the contract's verify/gates/
    prove_red, read through the shared contract parser (ONE meaning).
    Absent block → empty plan + note; unreadable or broken block → the
    same (the engine's own contract handling surfaces the break at verify
    time; the snapshot must never kill the launch).
    """
    from ..unit_contract import parse_todo_contract

    try:
        raw = todo_md.read_bytes()
    except OSError as e:
        return {
            "todo_sha": "",
            "verify": [],
            "gates": [],
            "prove_red": [],
            "note": f"TODO file unreadable ({type(e).__name__})",
        }
    todo_sha = hashlib.sha256(raw).hexdigest()
    content = raw.decode("utf-8", errors="replace")
    try:
        contract, _unknown = parse_todo_contract(content)
    except ValueError as e:
        return {
            "todo_sha": todo_sha,
            "verify": [],
            "gates": [],
            "prove_red": [],
            "note": f"broken contract block: {str(e).splitlines()[0] if str(e) else e}",
        }
    if not contract:
        return {
            "todo_sha": todo_sha,
            "verify": [],
            "gates": [],
            "prove_red": [],
            "note": "no contract block — the unit declares no verify commands",
        }
    return {
        "todo_sha": todo_sha,
        "verify": [str(v) for v in (contract.get("verify") or [])],
        "gates": [str(g) for g in (contract.get("gates") or [])],
        "prove_red": [str(p) for p in (contract.get("prove_red") or [])],
        "note": "",
    }


def _todo_finished(project_dir: Path, todo_id: str) -> bool:
    """AUD05-07 + AUD02-08: a TODO counts as finished when it is archived
    AND not active again — restore_todo leaves done/{id}/ behind, so
    ``is_archived`` alone would let a restored (active again) TODO pass."""
    active_ids = todos.list_active_todos(
        paths.inbox(project_dir), paths.outbox(project_dir)
    )
    return todos.is_archived(project_dir, todo_id) and todo_id not in active_ids


def _completed_view(project_dir: Path, state: dict) -> list[str]:
    """REPORTS26 B3 (TODO-0158): ``completed`` as the surfaces must show it.

    A run credits the finished ``current`` to ``completed`` only at the NEXT
    launch (the run_next advance mutator) — so right after approve the
    surfaces lag one cycle behind: the current TODO sits in done/ (and is
    committed) while the status shows it outside ``completed`` (the owner
    report: current TODO-0006, completed []). This view applies run_next's
    own credit rule (:func:`_todo_finished`) at READ time — it returns the
    list a supervisor should see and writes nothing: index/current stay
    where run_next owns them.
    """
    completed = [str(c) for c in (state.get("completed") or [])]
    current = str(state.get("current") or "")
    if current and current not in completed and _todo_finished(project_dir, current):
        completed.append(current)
    return completed


def run_brief(project_dir: Path) -> dict | None:
    """Compact run state for status/dashboard. None when no run state exists.

    ORCH M1.2: reads through the shared record reader
    (``awf/run_plan_read.py``) — the same parse the brief card and the
    supervisor context use (one path, no duplicate parsing)."""
    from ..run_plan_read import read_run_record
    from ..stall_detect import detect_stage_stall, stall_line

    record = read_run_record(project_dir)
    state = record.state
    if not state:
        return None
    # the sanitized budget from the shared record (a corrupt value already
    # degraded to 0 with a warning — no re-derivation from the raw state)
    budget = record.budget_minutes
    elapsed = run_state.elapsed_minutes(state)
    downtime = run_state.downtime_minutes(state)
    productive = run_state.productive_minutes(state)
    # B2: the budget is in PRODUCTIVE minutes — wall clock minus recorded
    # downtime (checkpoint waits, salvage handling, net-backoff pauses).
    left = max(0, int(budget - productive)) if budget else 0
    # RUN3 #1: which pipeline the run launches + how many exist. Degrades
    # to None/0 on a broken project instead of hiding the whole brief.
    try:
        from ..pipeline import active_pipeline_name, list_pipeline_names

        pipeline_info = {
            "active_pipeline": active_pipeline_name(project_dir),
            "pipeline_count": len(list_pipeline_names(project_dir)),
        }
    except Exception:
        pipeline_info = {"active_pipeline": None, "pipeline_count": 0}
    return {
        "active": bool(state.get("active")),
        "position": run_state.position(state),
        # RUN3 #2: the queue with per-item pipelines ({"todo_id",
        # "pipeline"}; empty pipeline = the config default).
        "queue": [dict(q) for q in (state.get("queue") or [])],
        "note": str(state.get("note") or ""),
        "current": state.get("current", ""),
        # REPORTS26 B3 (TODO-0158): the finished current is credited at
        # read time (run_next owns the position itself).
        "completed": _completed_view(project_dir, state),
        "budget_minutes": budget,
        "budget_left_minutes": left,
        "elapsed_minutes": int(elapsed),
        "downtime_minutes": int(downtime),
        "productive_minutes": int(productive),
        "stop_reason": state.get("stop_reason", ""),
        "report_file": state.get("report_file", ""),
        "no_checkpoints": bool(state.get("no_checkpoints")),
        # ORCH M4.2: the stalled-stage warning ("" when off / no stall) —
        # awf_status surfaces it inside the run block.
        "stall": stall_line(detect_stage_stall(project_dir)),
        **pipeline_info,
    }


def _normalize_plan(goal: str, criteria: list[str] | None) -> tuple[str, list[str]]:
    """ORCH M1.1: the run plan — ``goal`` is a plain string, ``criteria`` a
    list of non-empty strings. A single string criteria is accepted as one
    item (the MCP surface is string-shaped; the list is the canonical
    form). Whitespace is trimmed, empties dropped."""
    g = str(goal or "").strip()
    if criteria is None:
        items: list[str] = []
    elif isinstance(criteria, str):
        items = [criteria]
    else:
        items = list(criteria)
    return g, [str(c).strip() for c in items if str(c).strip()]


def run_start(
    project_dir: Path,
    *,
    queue: list[str | dict] | None = None,
    budget_minutes: int = 0,
    stop_flags: dict[str, list[str]] | None = None,
    note: str = "",
    force: bool = False,
    no_checkpoints: bool = False,
    goal: str = "",
    criteria: list[str] | None = None,
) -> RunStartResult:
    """Start an autonomous run: record the queue and the mechanical gates.

    The queue is a list of TODO ids (legacy) and/or objects
    ``{"todo_id": "TODO-NNNN", "pipeline": "<name>"}`` (RUN3 #2 — each item
    can pin its own pipeline; empty/absent ``pipeline`` = the config
    default). The state stores the canonical ``{"todo_id", "pipeline"}``
    form; old string-only state files keep reading. The supervisor writes
    each TODO file before calling :func:`run_next` for it; stop flags keyed
    by TODO id mark items awf must never auto-continue past (phase
    boundaries, external audits, owner-decision tasks).

    ``no_checkpoints`` (B4): when True, the BD-36 plan checkpoint is skipped
    for every pipeline launch of this run — the run does not expect the
    owner at every TODO. The flag is stored in the run state and surfaced
    by :func:`run_status` / :func:`run_brief`.

    ORCH M1.1 — the run plan: ``goal`` (one line) and ``criteria`` (a list
    of lines) are stored in the run state (a DERIVED RunPlan, one source —
    no second store) and surfaced by :func:`run_status` and the RUN-REPORT.
    Old run.yaml files without these fields read as before (no migration);
    a fresh run always resets them (``decisions`` included).
    """
    project_dir = _require_run_project(project_dir)
    items = _validate_queue(queue)
    ids = [i["todo_id"] for i in items]
    goal_clean, criteria_clean = _normalize_plan(goal, criteria)

    flags = {str(k): list(v) for k, v in (stop_flags or {}).items() if k}
    outcome: dict = {}

    def _start_mutator(st: dict) -> dict:
        # A-13: the inactive → active transition is checked AND written in
        # ONE lock hold. Before: the active check read the state BEFORE
        # the lock and the write had no condition — two concurrent
        # run_start(force=False) both passed the guard and the second one
        # silently clobbered the first queue.
        if st.get("active") and not force:
            outcome["conflict"] = st
            return st
        outcome["replaced"] = bool(st.get("active"))
        new_state = dict(st)
        new_state.update(
            active=True,
            # AUD02-11: project_root was written here but never read — a
            # dead key is a false contract signal. The run state file
            # already lives under the project's .agentic/, so the root is
            # implicit.
            queue=items,
            index=0,
            current="",
            completed=[],
            rejects={},
            outcomes={},
            stop_flags=flags,
            budget_minutes=int(budget_minutes or 0),
            # B2: a fresh run has a fresh downtime counter — a force
            # replace merges over the previous run.yaml, so the counter is
            # reset here or the old run's downtime would leak into the new
            # budget.
            downtime_seconds=0,
            started_at=run_state.now_iso(),
            # TODO-0178: the owner-idle accounting starts with the run —
            # the first heartbeat reference is "now", the downtime
            # snapshot is fresh (a force replace must not inherit the
            # previous run's beat, or its gap would be credited here).
            last_supervisor_beat=run_state.now_iso(),
            downtime_seconds_at_last_beat=0,
            # A-13: the run generation — identity of the run for the
            # conditional run_next transitions. A fresh run is always one
            # older than whatever existed (absent = 0).
            generation=run_state.generation_of(st) + 1,
            stop_reason="",
            report_file="",
            note=note.strip(),
            no_checkpoints=bool(no_checkpoints),
            # ORCH M1.1: the run plan — goal + criteria, reset with the
            # run (a force replace must not inherit the previous run's
            # plan), and a fresh causal memory (decisions belong to the
            # run that made them).
            goal=goal_clean,
            criteria=criteria_clean,
            decisions=[],
            # ORCH M2.3: the per-item evidence plans belong to the run that
            # launched them — a fresh run starts with an empty memory, a
            # force replace must not inherit the previous run's plans.
            evidence_plans=[],
        )
        return new_state

    state = run_state.update_run(project_dir, _start_mutator)

    if "conflict" in outcome:
        existing = outcome["conflict"]
        age = int(run_state.elapsed_minutes(existing))
        raise AwfApiError(
            f"Run already active ({run_state.position(existing)}, current "
            f"{existing.get('current') or '—'}, started {age} min ago). "
            f"Finish it with awf_run_finish, or pass force=true to replace it."
        )
    replaced = bool(outcome.get("replaced"))

    budget_note = f", budget {int(budget_minutes)} min" if budget_minutes else ""
    replace_note = " (previous run replaced)" if replaced else ""
    return RunStartResult(
        active=True,
        queue=items,
        position=run_state.position(state),
        budget_minutes=int(budget_minutes or 0),
        stop_flags=flags,
        message=(
            f"Run started in {project_dir}{replace_note}: {len(ids)} TODO(s)"
            f"{budget_note} — {', '.join(ids)}"
        ),
        next_action=(
            f"Write the first TODO (inbox/{ids[0]}.md if missing), then call "
            f"awf_run_next to launch it. Loop: awf_wait_for_event → on verify run "
            f"your own probes → awf_approve(evidence=...) or awf_reject → "
            f"awf_run_next. The stop-list is enforced by awf, not by you."
        ),
    )


def run_note(project_dir: Path, text: str) -> RunStatusResult:
    """Set the run's live description (R5): what the current stage is doing.

    The supervisor owns this text — awf renders it on the dashboard so the
    owner can see whether the run is alive without asking. Returns the
    refreshed status.
    """
    project_dir = _require_run_project(project_dir)
    state = run_state.read_run(project_dir)
    if not state or not state.get("active"):
        raise AwfApiError("No active run — nothing to annotate. Start one with awf_run_start.")
    run_state.write_run(project_dir, note=str(text).strip())
    return run_status(project_dir)


def supervisor_beat(project_dir: Path) -> dict:
    """TODO-0178: the supervisor heartbeat — "the owner is present".

    Called by the plugin's ``_exec`` wrapper on EVERY awf-* tool call,
    so the active main run's owner-idle accounting (gaps between beats
    → ``downtime_seconds``) sees the supervisor's real activity.

    Never raises and never changes the caller's result: a beat failure
    degrades to a silent no-op (the accompanying tool call must not
    break because of the accounting). Returns ``{"status": "ok"}``.
    """
    try:
        run_state.supervisor_beat(project_dir)
    except Exception:
        pass
    return {"status": "ok"}


def run_status(project_dir: Path) -> RunStatusResult:
    """Current run state (or an inactive summary when no run exists).

    ORCH M1.2: reads through the shared record reader
    (``awf/run_plan_read.py``) — the same parse the brief card and the
    supervisor context use (one path, no duplicate parsing).

    ORCH M4.2: the ``stall`` field carries the stalled-stage warning
    ("" when off / no stall / no active run) — the same line the brief
    card shows (one shared detector)."""
    from ..run_plan_read import read_run_record
    from ..stall_detect import detect_stage_stall, stall_line

    record = read_run_record(project_dir)
    state = record.state or {}
    active = record.active
    budget = record.budget_minutes
    elapsed = run_state.elapsed_minutes(state) if state else 0.0
    downtime = run_state.downtime_minutes(state) if state else 0.0
    productive = run_state.productive_minutes(state) if state else 0.0
    # B2: the budget is in PRODUCTIVE minutes (elapsed − recorded downtime).
    left = record.budget_left_minutes if budget else 0
    current = record.current
    position = record.position

    # ORCH M4.2: the stalled-stage warning — only inside an active run
    # (this surface talks about the run; outside it there is nothing to
    # stall against). The same detector/line as the brief card.
    stall = stall_line(detect_stage_stall(project_dir)) if active else ""

    if not state:
        message = "No run found — start one with awf_run_start(queue=[...])."
    elif active:
        message = (
            f"Run active: {position}, current {current or '—'}"
            + _current_pipeline_hint(state.get("queue"), current)
            + (f", budget left ~{left} min (productive)" if budget else "")
            + (f" Stage stall: {stall}." if stall else "")
        )
    else:
        message = (
            f"Run finished: {position}"
            + (f" — {state.get('stop_reason')}" if state.get("stop_reason") else "")
        )

    return RunStatusResult(
        active=active,
        position=position,
        queue=list(state.get("queue") or []),
        index=int(state.get("index", 0) or 0),
        current=current,
        # REPORTS26 B3 (TODO-0158): the finished current is credited at
        # read time (run_next owns the position itself).
        completed=_completed_view(project_dir, state),
        rejects=dict(state.get("rejects") or {}),
        stop_flags=dict(state.get("stop_flags") or {}),
        budget_minutes=budget,
        budget_left_minutes=left,
        elapsed_minutes=int(elapsed),
        downtime_minutes=int(downtime),
        productive_minutes=int(productive),
        stop_reason=str(state.get("stop_reason", "") or ""),
        report_file=str(state.get("report_file", "") or ""),
        message=message,
        no_checkpoints=bool(state.get("no_checkpoints")),
        # ORCH M1.1: the run plan + causal memory. The shared reader
        # sanitizes the fields, so absent (old state) / broken values
        # read as ""/[]/[] (record.state is the sanitized state).
        goal=record.goal,
        criteria=list(record.criteria),
        decisions=[dict(d) for d in record.decisions],
        stall=stall,
    )


def _salvage_events(project_dir: Path, since_iso: str) -> int:
    """SPEC A-run (second tier): silent worker deaths during the run.

    Counts ``salvage path`` lines in orchestrator.log with a timestamp at or
    after ``since_iso`` (run start). Used by the report as a health signal.
    """
    log = paths.agentic_dir(project_dir) / "logs" / "orchestrator.log"
    if not log.is_file():
        return 0
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    since = since_iso.rstrip("Z")
    pattern = re.compile(
        r"\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\].*salvage path"
    )
    return sum(1 for m in pattern.finditer(text) if m.group(1) >= since)


def _goal_check_lines(state: dict, reason: str, salvage: int) -> list[str]:
    """ORCH M4.2: the "Goal vs done" reconciliation section of the
    RUN-REPORT — the report answers «цель → что исполнено → риски →
    следующий шаг» (IMPLEMENTATION-STRATEGY §2).

    Goal and criteria come from the run state (the shared reader's
    sanitized values: a broken field already reads as ""/[] — then the
    section is skipped with a marker line, no error, M1 style). The
    queue fact is mechanical (credited ``completed`` vs the queue); the
    risks are the run's own health signals (salvage events counted by
    :func:`_salvage_events`, the rejects, the stop reason); the next
    step is next_action-style — it names the exact command.
    """
    lines = ["## Goal vs done", ""]
    goal = str(state.get("goal") or "").strip()
    criteria = [str(c).strip() for c in (state.get("criteria") or []) if str(c).strip()]
    if not goal and not criteria:
        lines.append(
            "- no goal or criteria recorded in the run plan — reconciliation "
            "skipped (awf_run_start accepts goal/criteria)."
        )
        lines.append("")
        return lines
    if goal:
        lines.append(f"**Goal:** {goal}")
    if criteria:
        lines.append(f"**Criteria:** {', '.join(criteria)}")
    queue = list(state.get("queue") or [])
    total = len(queue)
    completed = [str(c) for c in (state.get("completed") or [])]
    remaining: list[str] = []
    for item in queue:
        tid = str(item.get("todo_id", "")) if isinstance(item, dict) else str(item)
        if tid and tid not in completed:
            remaining.append(tid)
    queue_line = f"**Queue:** {len(completed)}/{total} done"
    if completed:
        queue_line += f" — completed: {', '.join(completed)}"
    if remaining:
        queue_line += f"; remaining: {', '.join(remaining)}"
    lines.append(queue_line + ".")
    risks: list[str] = []
    if salvage:
        risks.append(f"salvage events: {salvage}")
    rejects = state.get("rejects") or {}
    if rejects:
        risks.append("rejects: " + ", ".join(f"{k}×{v}" for k, v in rejects.items()))
    if str(reason or "").strip():
        risks.append(f"stop: {str(reason).strip()}")
    lines.append("**Risks:** " + ("; ".join(risks) if risks else "none recorded"))
    if remaining:
        first = remaining[0]
        lines.append(
            f"**Next step:** `awf_run_next` — launches {first} "
            f"({len(remaining)} left in the queue). The goal check stays "
            "yours: the queue ending does not mean the goal is met."
        )
    else:
        lines.append(
            "**Next step:** queue exhausted — judge the goal against the "
            "criteria: if met, the run is closed; if not, start a new run "
            "(`awf_run_start`) with the remaining criteria."
        )
    lines.append("")
    return lines


def _write_report(
    project_dir: Path,
    state: dict,
    reason: str,
    summary: str = "",
    forced: bool = False,
) -> Path | None:
    """Write RUN-REPORT-{ts}.md to outbox. Returns the path (or None on OSError).

    REPORTS29 (TODO-0170): when the state still names a ``current`` unit,
    the report carries the honest facts — archived or not, pipeline
    alive or dead — and, for a forced close over an unfinished unit, the
    explicit "closed with unfinished unit TODO-NNNN (force)" mark.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outbox = paths.outbox(project_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    report = outbox / f"RUN-REPORT-{ts}.md"

    queue = list(state.get("queue") or [])
    completed = list(state.get("completed") or [])
    rejects = state.get("rejects") or {}
    outcomes = state.get("outcomes") or {}
    rejects_note = ", ".join(f"{k}×{v}" for k, v in rejects.items()) or "—"
    try:
        budget = int(state.get("budget_minutes", 0) or 0)
    except (TypeError, ValueError):
        # ORCH M1.3 (class NEG-3): the report is written from the SAME
        # (possibly corrupt) state the stop gate saw — a corrupt budget
        # degrades to a 0 display here; the gate itself stops the run.
        budget = 0
    salvage = _salvage_events(project_dir, str(state.get("started_at") or ""))
    health_note = ""
    if salvage >= 3:
        health_note = (
            f" ⚠️ health: {salvage} silent worker deaths during the run — "
            f"the pipeline is unhealthy, consider not running autonomously further."
        )
    lines = [
        f"# Run report ({ts})",
        "",
        f"**Reason:** {reason}",
        f"**Queue:** {', '.join(_queue_item_label(q) for q in queue) or '—'}"
        f" — {len(completed)}/{len(queue)} done",
        f"**Completed:** {', '.join(completed) or '—'}",
        f"**Rejects:** {rejects_note}",
        f"**Salvage events:** {salvage}{health_note}",
        f"**Elapsed:** {int(run_state.elapsed_minutes(state))} min "
        f"(budget {state.get('budget_minutes', 0)} min)",
    ]
    # REPORTS29 (TODO-0170): the current unit's honest facts — the report
    # must not say "8/8 done" while the 8th unit is not archived.
    current_unit = str(state.get("current") or "")
    if current_unit:
        unit_archived = _unit_archived(project_dir, current_unit)
        unit_running, unit_pid, _unit_source = _liveness.resolve(project_dir)
        pipe = f"running (pid {unit_pid})" if unit_running else "not running"
        if unit_archived:
            lines.append(f"**Unit:** {current_unit} archived (done/{current_unit}/TODO.md)")
            lines.append(f"**Pipeline:** {pipe}")
        else:
            lines.append(
                f"**Unit:** pipeline is {pipe}; unit {current_unit} NOT archived"
            )
        if forced:
            lines.append(
                f"**Closed with loss:** closed with unfinished unit "
                f"{current_unit} (force)"
            )
    # ORCH M1.1: the run plan — goal and criteria, when set (old runs and
    # runs started without a plan show nothing, as before).
    goal = str(state.get("goal") or "").strip()
    criteria = [
        str(c).strip() for c in (state.get("criteria") or []) if str(c).strip()
    ]
    if goal:
        lines.append(f"**Goal:** {goal}")
    if criteria:
        lines.append(f"**Criteria:** {', '.join(criteria)}")
    # B2: the budget is in productive minutes (elapsed − downtime); show the
    # split so the owner sees how much of the run was incident, not work.
    if budget:
        lines.append(
            f"**Budget:** {int(run_state.productive_minutes(state))} of {budget} min "
            f"productive, {int(run_state.downtime_minutes(state))} min downtime"
        )
    lines += [""]
    if outcomes:
        ctx_dir = paths.context_dir(project_dir)
        lines += ["## Run diary (verdicts)", ""]
        for todo, verdict in outcomes.items():
            if isinstance(verdict, dict):
                note = verdict.get("reason") or ""
                extra = ""
                if verdict.get("verdict") == "approved":
                    # U11: surface both audit facts — what was checked
                    # (evidence) and on which tree (verified-sha).
                    bits: list[str] = []
                    ev = ctx_dir / f"RUN-EVIDENCE-{todo}.md"
                    if ev.is_file():
                        bits.append(f"evidence: {ev.name}")
                    vf = ctx_dir / f"VERIFIED-{todo}.sha"
                    if vf.is_file():
                        bits.append(f"verified: {vf.name}")
                    extra = f" ({', '.join(bits)})" if bits else ""
                lines.append(
                    f"- {todo}: {verdict.get('verdict', '?')}"
                    + (f" — {note[:200]}" if note else "")
                    + extra
                )
            else:
                lines.append(f"- {todo}: {verdict}")
        lines.append("")
    # ORCH M1.1: the causal memory — WHY the supervisor approved/rejected,
    # so the owner can audit the run after the fact. The approve entries
    # carry the evidence excerpt (the full text stays in RUN-EVIDENCE).
    decisions = [d for d in (state.get("decisions") or []) if isinstance(d, dict)]
    if decisions:
        lines += ["## Run decisions (causal memory)", ""]
        for d in decisions:
            d_reason = str(d.get("reason") or "")
            lines.append(
                f"- {str(d.get('ts') or '')} {str(d.get('kind') or '?')} "
                f"{str(d.get('todo_id') or '?')}"
                + (f" — {d_reason[:200]}" if d_reason else "")
            )
        lines.append("")
    # ORCH M4.2: the goal reconciliation — the report answers «цель →
    # что исполнено → риски → следующий шаг». The section is always
    # present (a run without a plan shows the marker, invariant 2).
    lines += _goal_check_lines(state, reason, salvage)
    if summary:
        lines += ["## Supervisor summary", "", summary, ""]
    try:
        atomic_write_text(report, "\n".join(lines))
    except OSError:
        return None
    return report


def stop_run(
    project_dir: Path,
    state: dict,
    reason: str,
    summary: str = "",
) -> RunNextResult:
    """Stop the run: write the report, mark inactive, guide the supervisor."""
    report = _write_report(project_dir, state, reason, summary)
    report_str = str(report) if report else ""
    run_state.write_run(
        project_dir, active=False, stop_reason=reason, report_file=report_str,
    )
    return RunNextResult(
        action="stopped",
        todo_id="",
        message=f"Run STOPPED: {reason}." + (f" Report: {report}" if report else ""),
        report_file=report_str,
        next_action=(
            "Notify the owner: what happened, what is done, what is needed. "
            "Do not continue the run."
        ),
    )


def _reservation_owner() -> str:
    """TODO-0189 (A-13 residual angle): the owner of a queue-position
    reservation — the (pid, thread) that reserved it.

    The reservation (``run.yaml`` ``current``) used to carry no owner: the
    takeover path (TODO-0103) re-reserved the same value as a no-op, and a
    refused launch's release matched the (generation, index, current) CAS
    tuple of ANOTHER caller's live reservation and clobbered it. The stamp
    makes the capture visible (the taker re-stamps it) and the release
    owner-checked: it removes only its own reservation.
    """
    return f"{os.getpid()}:{threading.get_ident()}"


def run_next(
    project_dir: Path,
    *,
    from_stage: str | None = None,
    auto: bool = False,
    timeout: int = 3600,
    background: bool = True,
) -> RunNextResult:
    """Launch the next queue item, or stop when a gate fires.

    Gates (in order): no active run → refused; queue exhausted / budget
    exceeded / stop flag / rejected twice → run stops with a report; previous
    TODO not archived → refused; TODO file missing → refused (the supervisor
    must write it first).
    """
    project_dir = _require_run_project(project_dir)

    state = run_state.read_run(project_dir)
    if not state or not state.get("active"):
        return RunNextResult(
            action="refused",
            todo_id="",
            message="No active run.",
            next_action="Start one with awf_run_start(queue=[...]).",
        )

    queue = list(state.get("queue") or [])
    index = int(state.get("index", 0) or 0)

    if index >= len(queue):
        # AUD02-08: the last item is normally credited to `completed` at the
        # NEXT launch — which never comes at the end of the queue. Credit it
        # here, but only when it is really finished (archived and not active
        # again) — the report must not overstate.
        current = str(state.get("current") or "")
        completed = list(state.get("completed") or [])
        if current and current not in completed and _todo_finished(project_dir, current):
            completed.append(current)
            state = run_state.write_run(project_dir, completed=completed)
        return stop_run(project_dir, state, "queue exhausted — all items processed")

    try:
        budget = int(state.get("budget_minutes", 0) or 0)
    except (TypeError, ValueError):
        # ORCH M1.3 (class NEG-3): a corrupt budget_minutes in an ACTIVE
        # run.yaml (hand-edited; the write path stores an int) makes the
        # budget gate unverifiable. Degrade toward safety, the AUD02-09
        # direction: stopping is safe, degrading to 0 would silently
        # disable the gate (and the raw ValueError would kill the run loop).
        return stop_run(
            project_dir, state,
            "run state corrupted (budget_minutes unparseable) — "
            "the budget gate cannot be verified",
        )
    if budget:
        if not run_state.started_at_ok(state):
            # AUD02-09: a corrupt started_at used to read as elapsed 0.0 —
            # the budget gate silently disabled. Degrade toward safety:
            # the run clock is untrustworthy, so the run stops.
            return stop_run(
                project_dir, state,
                "run state corrupted (started_at unparseable) — "
                f"budget {budget} min cannot be verified",
            )
        # B2: the budget is in PRODUCTIVE minutes — wall clock minus recorded
        # downtime (checkpoint waits, salvage handling, net-backoff pauses).
        # The run is a guard against "the autonomous run lives forever", not
        # a punishment for incidents.
        if run_state.productive_minutes(state) > budget:
            return stop_run(project_dir, state, f"budget exhausted ({budget} min)")

    # RUN3 #2: queue items are normalized {"todo_id", "pipeline"} dicts
    # (read_run upgrades legacy strings); the defensive isinstance keeps
    # hand-written state files alive too.
    next_item = queue[index]
    next_id = (
        str(next_item.get("todo_id", "")) if isinstance(next_item, dict) else str(next_item)
    )

    flags = (state.get("stop_flags") or {}).get(next_id) or []
    if flags:
        return stop_run(
            project_dir, state, f"stop-list flag on {next_id}: {', '.join(map(str, flags))}",
        )

    rejects = state.get("rejects") or {}
    if int(rejects.get(next_id, 0) or 0) >= 2:
        return stop_run(
            project_dir, state,
            f"{next_id} was rejected twice — the task itself needs the owner",
        )

    if index > 0:
        prev_item = queue[index - 1]
        prev = (
            str(prev_item.get("todo_id", ""))
            if isinstance(prev_item, dict)
            else str(prev_item)
        )
        # AUD05-07: restore_todo leaves done/{id}/ behind, so is_archived alone
        # lets a restored (active again) prev pass. Require it to be gone from
        # the active list too (see _todo_finished).
        if not _todo_finished(project_dir, prev):
            # ORCH M1.3: when the open block is a counted REJECT, say so —
            # the generic "verify → approve/reject" advice is wrong there
            # (the approve is refused against a counted reject; a second
            # reject stops the run). The permitted step is to close the
            # reject decision; a fresh session must not guess it.
            try:
                prev_rejects = int(rejects.get(prev, 0) or 0)
            except (TypeError, ValueError):
                prev_rejects = 0
            if prev_rejects >= 1:
                return RunNextResult(
                    action="refused",
                    todo_id=next_id,
                    message=(
                        f"Previous TODO {prev} was rejected ({prev_rejects}×) "
                        f"and the decision is not closed — the run will not "
                        f"launch {next_id} over an unreviewed result."
                    ),
                    next_action=(
                        f"Close the reject decision: re-plan the task (issue a "
                        f"new TODO) and retire {prev} (`awf_todo_retire`), or "
                        f"stop the run (`awf_run_finish`) if it needs the "
                        f"owner. Reason: outbox/REVIEW-{prev}.md."
                    ),
                )
            return RunNextResult(
                action="refused",
                todo_id=next_id,
                message=f"Previous TODO {prev} is not finished (not archived yet).",
                next_action=(
                    "Finish the current iteration first (verify → approve/reject), "
                    "then awf_run_next again."
                ),
            )

    todo_md = paths.inbox(project_dir) / f"{next_id}.md"
    if not todo_md.is_file() or todo_md.stat().st_size == 0:
        return RunNextResult(
            action="refused",
            todo_id=next_id,
            message=f"{next_id}.md is missing or empty — looked in {todo_md}",
            next_action=(
                f"Write .agentic/inbox/{next_id}.md (the task for this queue item), "
                f"then call awf_run_next again."
            ),
        )

    # ORCH M2.3: the evidence plan snapshot — the launched TODO's contract
    # (verify/gates/prove_red) + file hash. It lands in the run record in
    # the SAME atomic write as the position commit (the advance mutator
    # below, the decisions' path M1), so a refused launch records nothing
    # and a later edit/retire of the TODO cannot lose the plan.
    evidence_plan = _todo_evidence_plan(todo_md)

    # RUN3 #2: the item's own pipeline, else the TODO-declared one (Part B),
    # else the config default (None = unchanged behavior). Resolved BEFORE
    # any side effect (baseline, .ready): a refused launch must leave the
    # tree untouched — a stale {id}.ready would mark the unlaunched TODO
    # active (todos.list_active) and a stray awf_start could pick it up
    # with the config pipeline instead of the pinned one.
    pipeline_name = (
        str(next_item.get("pipeline", "") or "").strip()
        if isinstance(next_item, dict)
        else ""
    )
    if not pipeline_name:
        pipeline_name = _todo_declared_pipeline(todo_md)
    launch_pipeline: str | None = None
    if pipeline_name:
        # RUN3 #1: reuse the shared resolver — an explicit name that does not
        # exist raises AwfApiError with the list of available pipelines
        # (no duplicate of that logic here).
        from ..pipeline import resolve_pipeline_file

        try:
            resolve_pipeline_file(project_dir, pipeline_name)
        except AwfApiError as e:
            return RunNextResult(
                action="refused",
                todo_id=next_id,
                message=f"Cannot launch {next_id}: {e}",
                next_action=(
                    f"Fix the queue entry (or the {next_id} front-matter "
                    "'pipeline:') to an existing pipeline, then retry "
                    "awf_run_next."
                ),
            )
        launch_pipeline = pipeline_name

    # A-13: the queue position is RESERVED before the launch window. The
    # reservation, the commit below, and the release on refusal all compare
    # (generation, index, current) — a concurrent run_next (or a force
    # replace) can no longer clobber the launch: the loser's CAS misses and
    # it refuses with the index untouched.
    # TODO-0189 (A-13 residual angle): the reservation also carries its
    # OWNER (``reserved_by`` stamp) — the release below matches it, so a
    # refused launch cannot clobber a reservation captured by another
    # caller (the takeover path re-stamps it with its own identity).
    gen = run_state.generation_of(state)
    cur = str(state.get("current", "") or "")
    me = _reservation_owner()

    # TODO-0103 (A-13 hole): the pre-read can land AFTER an earlier
    # run_next's reservation write — then cur already equals next_id and
    # the reservation CAS below would MATCH the on-disk state (its tuple
    # was computed from the reserved snapshot) and "reserve" the same
    # position a second time: two launches for one queue item (the flake
    # observed in waves 5/5b). Resolve the collision by the liveness of
    # the pipeline that should own the position — the existing single
    # resolver (awf.api._liveness): alive → the launch is in progress,
    # refuse; dead → the owner crashed between reservation and launch —
    # take over the dead reservation. A flat "refuse when cur == next_id"
    # is deliberately NOT used: it would lock the queue after a crash
    # (QA probe B, TODO-0082).
    if cur == next_id:
        running, _pid, _source = _liveness.resolve(project_dir)
        if running:
            return RunNextResult(
                action="refused",
                todo_id=next_id,
                message=(
                    f"Cannot launch {next_id}: this queue position is "
                    "already reserved and its pipeline is live — the "
                    "launch is in progress (or already launched). The "
                    "index is not moved."
                ),
                next_action=(
                    "Check awf_status for the live pipeline; retry "
                    "awf_run_next once the position is free."
                ),
            )
        # dead reservation — fall through: the re-reservation below
        # (cur == next_id) matches the on-disk state and takes the
        # position over.

    def _reserve_mutator(st: dict) -> dict:
        st["current"] = next_id
        # TODO-0189: the owner stamp — the takeover path falls through to
        # this same mutator, so a capture re-stamps the reservation with
        # the taker's identity (the capture becomes visible on disk).
        st["reserved_by"] = me
        return st

    _, reserved = run_state.update_run_cas(
        project_dir,
        _reserve_mutator,
        generation=gen,
        index=index,
        current=cur,
    )
    if not reserved:
        return RunNextResult(
            action="refused",
            todo_id=next_id,
            message=(
                f"Cannot launch {next_id}: the run changed between the gate "
                "check and the launch reservation (another run_next owns "
                "this queue position, or the run was replaced). The index "
                "is not moved."
            ),
            next_action="Check awf_run_status, then retry awf_run_next.",
        )

    def _release_reservation() -> None:
        # A-13: restore the pre-reservation `current` — only while we still
        # own the position (the same CAS tuple). A run closed or
        # force-replaced inside the launch window is not written back.
        # TODO-0189 (A-13 residual angle): the CAS also matches the owner
        # stamp — a reservation captured by another caller (takeover) or a
        # legacy unstamped one is not ours anymore and is left untouched.
        def _release_mutator(st: dict) -> dict:
            st["current"] = cur
            st.pop("reserved_by", None)
            return st

        run_state.update_run_cas(
            project_dir,
            _release_mutator,
            generation=gen,
            index=index,
            current=next_id,
            reserved_by=me,
        )

    # Baseline before the signal (same order as dispatch_todo).
    # AUD05-08: best-effort — ANY failure here (no commits, git timeout,
    # permissions) must not kill the run; the orchestrator ensures a
    # baseline at stage start.
    try:
        import subprocess

        from .pipeline import create_baseline

        # RUN5 #1 (leak-gate): this re-baseline would wipe the carry-over
        # exclusion dispatch set for a retry — re-apply it from the
        # BASELINE-{id}.carry_over link (the origin's recorded paths).
        # Link-read failure must not kill the re-baseline (best-effort).
        carry_over: set[str] | None = None
        link = paths.context_dir(project_dir) / f"BASELINE-{next_id}.carry_over"
        try:
            if link.is_file():
                origin = link.read_text(encoding="utf-8").strip()
                if origin:
                    from ..reject_files import read_reject_files

                    recorded = read_reject_files(project_dir, origin)
                    if recorded is not None:
                        carry_over = set(recorded)
        except OSError:
            carry_over = None
        # RUN10 #4 (TODO-0074): same for the inclusion list — re-apply the
        # dispatch's re-claimed paths from the BASELINE-{id}.include link,
        # or the re-baseline would silently drop them from the unit commit.
        # D-01/R-07: read_include_list is the single .include parser — the
        # helper's []-for-empty-file case is a no-op in create_baseline
        # (set(include or ())), identical to the old None. Link-read failure
        # must not kill the re-baseline (best-effort; the helper swallows
        # OSError itself, the outer catch is the backstop).
        from ..include_untracked import read_include_list

        include: set[str] | None = None
        recorded = read_include_list(project_dir, next_id)
        if recorded:
            include = set(recorded)
        create_baseline(project_dir, next_id, carry_over=carry_over, include=include)
    except (AwfApiError, RuntimeError, OSError, subprocess.SubprocessError):
        pass  # best-effort — the orchestrator ensures a baseline at stage start

    # A-21: remember the signal state BEFORE publishing — a refused launch
    # must restore the pre-launch state: no .ready before → none after the
    # refusal; a pre-existing one (dispatch, manual touch) stays. Without
    # this, a refused run_next leaves a stale {id}.ready behind and
    # newest_active marks the unlaunched TODO active.
    ready_path = paths.inbox(project_dir) / f"{next_id}.ready"
    had_ready = ready_path.exists()
    ready_path.touch()

    from .pipeline import start_pipeline

    # V-01/V-02 (audit re-verification 2026-09-27): ONE success condition
    # and ONE rollback for the launch window. Success: background — the
    # process really launched (run_mode="background"); foreground — the
    # run COMPLETED (exit_code == 0). Everything else (noop / error /
    # foreground non-zero exit / a raised exception) is a refusal that
    # rolls back only this call's own effects.
    launch_failed = True
    refusal_message = ""
    try:
        result = start_pipeline(
            project_dir,
            background=background,
            from_stage=from_stage,
            auto=auto,
            timeout=timeout,
            pipeline=launch_pipeline,  # RUN3 #2: per-item pipeline (None = config)
            todo_id=next_id,  # NEG-2026-09-19 R1: queue order is pinned, not "newest active"
            # RUN9 #3 (TODO-0067): the RUN flag — the engine skips the BD-36
            # checkpoint for no_checkpoints runs anyway (pipeline_engine
            # run_flag), so the pre-launch foreground guard must see it too or
            # the direct API path run_next(background=False) gets a false
            # refusal. Background behavior is unchanged (flag was a no-op there:
            # the engine's run_flag already covered the checkpoint).
            no_checkpoints=bool(state.get("no_checkpoints")),
        )
        launch_failed = result.run_mode in ("noop", "error") or (
            result.run_mode == "foreground" and (result.exit_code or 0) != 0
        )
        refusal_message = f"Launch failed: {result.message}"
    except Exception as e:
        # V-02: an unexpected exception in the launch window must not escape
        # with this call's own effects (the .ready, the reservation) still
        # in place — it becomes a classified refusal and rolls them back.
        refusal_message = (
            f"Launch failed: start_pipeline raised {type(e).__name__}: {e}"
        )

    if launch_failed:
        # AUD02-02: the launch failed — the run position stays put, so the
        # retry targets the same item instead of skipping it (and the
        # unlaunched TODO is not counted as completed).
        # A-13: the reservation is released too — a refused launch must
        # not leave the item owned by a run that launched nothing.
        # A-21: and the signal this launch created is rolled back — a
        # pre-existing .ready is left untouched. Best-effort like the
        # baseline above: a cleanup failure must not turn the refusal
        # into an error.
        if not had_ready:
            try:
                ready_path.unlink()
            except OSError:
                pass
        _release_reservation()
        return RunNextResult(
            action="refused",
            todo_id=next_id,
            message=refusal_message,
            next_action="Investigate the pipeline state (awf_status), then retry awf_run_next.",
        )

    # AUD02-02: advance the run position only AFTER a successful launch.
    # AUD05-05 (rest) + A-13: the commit is re-checked UNDER THE LOCK by
    # (generation, index, current) — and on `active`. If the run closed
    # (run_finish / stop_run) or was force-replaced during the launch
    # window, the item is NOT recorded: a closed run cannot own the verify
    # cycle of a launched pipeline. The orphaned pipeline is the accepted
    # consequence (documented in the result) — the owner inspects it via
    # awf_status and decides (kill + fresh run, or take it over).
    advance = {"applied": False}

    def _advance_mutator(st: dict) -> dict:
        if not st.get("active"):
            return st  # closed during the window — veto, nothing written
        completed = list(st.get("completed") or [])
        # RUN3 #2: queue items are {"todo_id", "pipeline"} dicts — credit
        # the previous item by its id string (prev, computed above).
        if index > 0 and prev not in completed:
            completed.append(prev)
        st["index"] = index + 1
        st["current"] = next_id
        st["completed"] = completed
        # ORCH M2.3: the evidence plan joins the SAME atomic write — one
        # lock hold, one file (the decisions' path, M1).
        run_state.append_evidence_plan(st, next_id, evidence_plan)
        advance["applied"] = True
        return st

    committed = run_state.update_run_cas(
        project_dir,
        _advance_mutator,
        generation=gen,
        index=index,
        current=next_id,
    )[1]

    if not committed or not advance["applied"]:
        # A-13: the ownership was lost in the launch window (the run
        # closed, or was force-replaced — the CAS tuple no longer matches)
        # — the reservation dies with it: restore the pre-launch state so
        # a closed run does not own a current item (the audit05 forbidden
        # combination) and a retry does not see a position someone else
        # no longer owns. A force-replaced state is untouched: the
        # release CAS misses, exactly as it must.
        _release_reservation()
        return RunNextResult(
            action="started",
            todo_id=next_id,
            message=(
                f"Run item {index + 1}/{len(queue)} launched: {next_id} "
                f"({result.run_mode}) — but the run was closed or replaced "
                "during the launch window, so index/current were NOT "
                "recorded. The launched pipeline is orphaned (accepted "
                "consequence): no run will finish its verify cycle."
            ),
            run_mode=result.run_mode,
            run_id=result.run_id,
            log_file=result.log_file or "",
            next_action=(
                "Inspect the orphaned pipeline with awf_status, then decide: "
                "awf_kill + a fresh run (awf_run_start) to take over its verify "
                "cycle, or finish the work manually."
            ),
        )

    # REPORTS26 F4 (TODO-0164): the launched unit runs INSIDE a run — the
    # standalone protocol ("GO IDLE — wait for user", awf_start) does not
    # apply: the supervisor must keep the run loop, not go idle. The
    # embedded pipeline message carries that sentence for background
    # launches — strip it so one answer carries one protocol, and
    # next_action names the run loop (phase-run.md) instead.
    detail = result.message.replace(". GO IDLE — wait for user.", "")
    return RunNextResult(
        action="started",
        todo_id=next_id,
        message=(
            f"Run item {index + 1}/{len(queue)} launched: {next_id} "
            f"({result.run_mode}). {detail}"
        ),
        run_mode=result.run_mode,
        run_id=result.run_id,
        log_file=result.log_file or "",
        next_action=(
            f"Run item {index + 1}/{len(queue)} launched ({next_id}). "
            "Continue the run loop: awf_wait_for_event(actionable_only=True, "
            "timeout=<suggested>) → verify → approve → done → awf_run_next."
        ),
    )


def _unit_archived(project_dir: Path, todo_id: str) -> bool:
    """REPORTS29 (TODO-0170): the unit's archive file on disk
    (``done/<id>/TODO.md`` — the stricter check: an empty done/<id>/
    from a failed archive is NOT archived)."""
    return (paths.done_dir(project_dir) / todo_id / "TODO.md").is_file()


def _leftover_signals(project_dir: Path, todo_id: str) -> list[str]:
    """REPORTS29 (TODO-0170): decision signals still lying for the unit
    (inbox APPROVE/ACK, outbox REVIEW) — project-relative paths."""
    found = []
    for p in (
        paths.inbox(project_dir) / f"APPROVE-{todo_id}.ready",
        paths.inbox(project_dir) / f"ACK-{todo_id}.ready",
        paths.outbox(project_dir) / f"REVIEW-{todo_id}.md",
    ):
        if p.is_file():
            found.append(p.relative_to(project_dir).as_posix())
    return found


def _finish_facts(project_dir: Path, current: str) -> dict:
    """REPORTS29 (TODO-0170): what the close gate must weigh for the
    current unit — archived on disk, stage state cleared, pipeline
    liveness. ``unfinished`` is the refusal condition."""
    archived = _unit_archived(project_dir, current)
    running, pid, _source = _liveness.resolve(project_dir)
    pstate = pipeline_state.read_state(project_dir)
    stage_cleared = pstate is None
    return {
        "archived": archived,
        "running": running,
        "pid": pid,
        "stage_cleared": stage_cleared,
        "stage": str((pstate or {}).get("stage_name") or ""),
        "stage_todo": str((pstate or {}).get("todo_id") or ""),
        "unfinished": (not archived) or (not stage_cleared) or running,
    }


def _finish_refusal(project_dir: Path, current: str, facts: dict) -> str:
    """REPORTS29 (TODO-0170): the refusal text — what exactly is left
    (pipeline alive/dead with pid, signals lying, tree status) and what
    to do (continue/approve/reject). One message: it is the supervisor's
    only surface."""
    signals = _leftover_signals(project_dir, current)
    tree = git_utils.status_porcelain(project_dir).strip()
    paths_changed = tree.splitlines()
    if paths_changed:
        shown = ", ".join(line.strip()[:48] for line in paths_changed[:3])
        more = f" +{len(paths_changed) - 3}" if len(paths_changed) > 3 else ""
        tree_fact = f"dirty ({len(paths_changed)} path(s): {shown}{more})"
    else:
        tree_fact = "clean"

    lines = [
        "run_finish refused: the run is ACTIVE and unit "
        f"{current} is not finished — nothing was closed, no report "
        "written. What is left:",
        (
            f"- pipeline: ALIVE (pid {facts['pid']})"
            if facts["running"]
            else "- pipeline: dead (no live pid)"
        ),
        (
            "- stage state: cleared"
            if facts["stage_cleared"]
            else "- stage state: NOT cleared "
            f"({facts['stage'] or '?'} for {facts['stage_todo'] or '—'})"
        ),
        (
            f"- unit: archived (done/{current}/TODO.md)"
            if facts["archived"]
            else f"- unit: NOT archived (done/{current}/TODO.md absent)"
        ),
        ("- signals lying: " + ", ".join(signals) if signals else "- signals: none lying"),
        f"- tree: {tree_fact}",
        "What to do:",
    ]
    if facts["running"]:
        lines.append(
            "- the pipeline is still running: wait for the stage to end "
            "(awf_wait_for_event) or stop it (awf_kill), then retry "
            "awf_run_finish"
        )
    if not facts["stage_cleared"]:
        lines.append(
            "- the pipeline is dead on a stage: resume the unit with "
            f"awf_continue (from {facts['stage'] or 'the last stage'} — "
            "a valid pre-existing APPROVE is consumed, REPORTS29)"
        )
    if any(s.endswith(f"APPROVE-{current}.ready") for s in signals):
        lines.append(
            f"- an APPROVE signal lies for {current}: approve the unit "
            "(awf_approve with evidence + verified_sha) — the commit gate "
            "archives it"
        )
    if any(s.endswith(f"REVIEW-{current}.md") for s in signals):
        lines.append(
            f"- a REVIEW rejection lies for {current}: fix and re-run the "
            "unit (awf_dispatch_todo + awf_start) or close the run on "
            "purpose (force=True)"
        )
    if not facts["archived"] and facts["stage_cleared"] and not facts["running"]:
        lines.append(
            f"- the unit {current} is not closed and nothing is waiting: "
            "finish the verify ritual (approve with evidence), or close "
            "with loss: awf_run_finish(force=True) — the RUN-REPORT will "
            "carry the 'closed with unfinished unit' mark"
        )
    return "\n".join(lines)


def run_finish(
    project_dir: Path,
    *,
    reason: str = "finished by supervisor",
    summary: str = "",
    force: bool = False,
) -> RunFinishResult:
    """Close the run: write the report and mark it inactive.

    REPORTS29 (TODO-0170): while the run is active and ``current`` is
    set, the close is REFUSED when the unit is not finished — not
    archived (``done/<id>/TODO.md`` absent), the stage state not cleared,
    or the pipeline alive. The refusal names what is left and what to do
    (see :func:`_finish_refusal`) and the run stays active.
    ``force=True`` is the conscious close with loss: the report is
    written and the run closed, the RUN-REPORT carries the
    "closed with unfinished unit TODO-NNNN (force)" mark.
    """
    project_dir = _require_run_project(project_dir)
    state = run_state.read_run(project_dir)
    if not state:
        return RunFinishResult(
            active=False,
            reason=reason,
            report_file="",
            message="No run state found — nothing to finish.",
        )
    current = str(state.get("current") or "")
    facts: dict = {}
    if state.get("active") and current:
        facts = _finish_facts(project_dir, current)
        if facts["unfinished"] and not force:
            return RunFinishResult(
                active=True,
                reason=reason,
                report_file="",
                message=_finish_refusal(project_dir, current, facts),
            )
    report = _write_report(
        project_dir, state, reason, summary,
        forced=force and bool(facts) and not facts.get("archived"),
    )
    report_str = str(report) if report else ""
    run_state.write_run(
        project_dir, active=False, stop_reason=reason, report_file=report_str,
    )
    return RunFinishResult(
        active=False,
        reason=reason,
        report_file=report_str,
        message=f"Run finished: {reason}." + (f" Report: {report}" if report else ""),
    )


def _revision_items(
    state: dict, requested: list[dict]
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """ORCH M3.4: classify the run queue + the requested changes.

    Queue lifecycle per item (the launch position from ``run_next``):
    positions ``< index`` are STARTED (the in-flight element sits at
    ``index - 1`` while ``current`` is set), positions ``>= index`` are
    NOT STARTED. ``completed`` ids count as completed regardless of
    position (the last item is credited only at exhaustion).

    Returns ``(current_queue, changes, conflicts, unchanged)``:
    - ``current_queue`` — every queue item with its lifecycle state
      (``completed`` | ``current`` | ``started`` | ``queued``);
    - ``changes`` — requested items that change a not-started element's
      pipeline (``{"todo_id", "from", "to"}``; ``""`` = config default);
    - ``conflicts`` — requested items that cannot be changed
      (current / started / completed / not in the queue);
    - ``unchanged`` — requested items whose pipeline already matches.
    """
    queue = list(state.get("queue") or [])
    try:
        index = int(state.get("index", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    current = str(state.get("current") or "")
    completed = {str(c) for c in (state.get("completed") or [])}

    def _item_fields(item: object) -> tuple[str, str]:
        if isinstance(item, dict):
            return (
                str(item.get("todo_id", "")),
                str(item.get("pipeline", "") or ""),
            )
        return str(item), ""

    def _lifecycle(tid: str, pos: int) -> str:
        if tid in completed:
            return "completed"
        if tid == current:
            return "current"
        if pos < index:
            return "started"
        return "queued"

    current_queue: list[dict] = []
    by_id: dict[str, tuple[int, str]] = {}
    for pos, item in enumerate(queue):
        tid, pipe = _item_fields(item)
        current_queue.append(
            {"todo_id": tid, "pipeline": pipe, "position": pos, "state": _lifecycle(tid, pos)}
        )
        by_id[tid] = (pos, pipe)

    conflicts: dict[str, str] = {
        "completed": "already completed",
        "current": "the current (in-flight) element",
        "started": "already started",
    }
    changes: list[dict] = []
    conflicts_list: list[dict] = []
    unchanged: list[dict] = []
    for req in requested:
        tid = req["todo_id"]
        to = str(req.get("pipeline", "") or "")
        entry = by_id.get(tid)
        if entry is None:
            conflicts_list.append({"todo_id": tid, "reason": "not in the run queue"})
            continue
        pos, pipe = entry
        lifecycle = _lifecycle(tid, pos)
        if lifecycle != "queued":
            conflicts_list.append({"todo_id": tid, "reason": conflicts[lifecycle]})
            continue
        if pipe == to:
            unchanged.append({"todo_id": tid, "pipeline": to})
        else:
            changes.append({"todo_id": tid, "from": pipe, "to": to})
    return current_queue, changes, conflicts_list, unchanged


def run_revise(
    project_dir: Path,
    *,
    queue: list[str | dict] | None = None,
    reason: str = "",
    key: str = "",
    preview: bool = False,
    stop_running: bool = False,
) -> RunReviseResult:
    """ORCH M3.4: revise the pipeline of the NOT-STARTED elements of the
    active run's queue — one typed operation with a preview and an
    idempotency key.

    - ``preview=True`` — no side effects: the current queue (per-item
      lifecycle state), which elements would change, the conflicts
      (current/started/completed/not in the queue), the key. The preview is
      not refused by a live engine (it is read-only).
    - apply (``preview=False``) — changes the pipeline of the not-started
      elements only; the completed and the current element are never
      touched. Refusals (each returns the current state, the previous plan
      stays in force): no active run; the engine is alive and
      ``stop_running`` is false (stop the unit first); ANY conflict in the
      request (atomic — nothing is applied); a missing ``key``; a
      generation mismatch (CAS, A-13 — the run changed between the read
      and the write); the stop failed (ORCH M4.1 — the stage survived the
      kill, so the revision is NOT applied and the retry is possible).
    - ``stop_running=True`` (ORCH M4.1) — the caller allows stopping the
      live unit: the stop goes through the engine's STANDARD kill path
      (``kill_pipeline``: TERM → grace → KILL the pipeline, the worker
      tree kill, the last-kill record — no home-grown signals). The resume
      point (state ``stage_name`` / ``todo_id``) is captured BEFORE the
      kill (which clears the state) and restored after, so a plain
      ``awf continue`` resumes the unit from the stopped stage; the answer
      and the revision record name it (``resume_from``). No live stage →
      the stop is a no-op and the revision applies.
    - a repeat with the same ``key`` is a no-op: the stored revision is
      returned, nothing is written twice (the check and the append are one
      step under one lock hold).
    - the applied revision is recorded in the run state (state/run.yaml,
      ``revisions``: ``{ts, key, kind: "revision", reason, changes,
      generation, resume_from}`` — the decisions' path) in the SAME CAS
      write as the queue change. The record's ``generation`` is the
      generation the revision was applied AGAINST (the CAS identity).
    - ORCH M6.3: an applied revision moves the run to a new CYCLE — the
      queue is a new plan, so the state's ``generation`` is bumped in the
      same CAS write (the record keeps the pre-bump value, see above).
      Approvals published for the pre-revision generation are stale: the
      commit gate refuses them (M2.1 binding, binding_refusal) and the
      supervisor re-verifies and re-approves on the current cycle.
    """
    project_dir = _require_run_project(project_dir)
    requested = _validate_queue(queue)
    reason_clean = str(reason or "").strip()
    key_clean = str(key or "").strip()

    state = run_state.read_run(project_dir)
    if not state or not state.get("active"):
        return RunReviseResult(
            action="refused",
            preview=bool(preview),
            key=key_clean,
            message=(
                "No active run — a revision revises the queue of an active "
                "run. Start one with awf_run_start(queue=[...])."
            ),
        )

    gen = run_state.generation_of(state)
    try:
        index = int(state.get("index", 0) or 0)
    except (TypeError, ValueError):
        index = 0
    current = str(state.get("current") or "")
    current_queue, changes, conflicts, unchanged = _revision_items(state, requested)

    if preview:
        bits = [
            f"{len(changes)} element(s) would change",
            f"{len(conflicts)} conflict(s)" if conflicts else "no conflicts",
        ]
        return RunReviseResult(
            action="preview",
            preview=True,
            key=key_clean,
            generation=gen,
            current_queue=current_queue,
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            message="Preview: " + ", ".join(bits) + " — nothing was written.",
            next_action=(
                "Apply with the same queue + key (preview=false). A stage must "
                "be stopped before the apply (a live engine refuses it)."
            ),
        )

    # A live engine (a stage is running) — refused with the stop-first
    # hint UNLESS the caller allows the stop (stop_running, ORCH M4.1).
    # The stop itself happens below the cheap state checks: a conflicted
    # or keyless request must not take down the running unit for nothing.
    running, _pid, source = _liveness.resolve(project_dir)
    if running and not stop_running:
        return RunReviseResult(
            action="refused",
            preview=False,
            key=key_clean,
            generation=gen,
            current_queue=current_queue,
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            message=(
                f"Refused: a stage is running (source: {source or '?'}) — "
                "stop the unit first (awf_kill), then retry the revision; "
                "or pass stop_running=true to stop it as part of this call "
                "(ORCH M4.1). The previous plan stays in force."
            ),
        )

    if conflicts:
        listed = "; ".join(f"{c['todo_id']}: {c['reason']}" for c in conflicts)
        return RunReviseResult(
            action="refused",
            preview=False,
            key=key_clean,
            generation=gen,
            current_queue=current_queue,
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            message=(
                f"Refused: the request touches elements that cannot be "
                f"changed — {listed}. Nothing was changed (atomic); the "
                "previous plan stays in force. Review the preview."
            ),
        )

    if not key_clean:
        return RunReviseResult(
            action="refused",
            preview=False,
            key="",
            generation=gen,
            current_queue=current_queue,
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            message=(
                "key is required to apply the revision — it is the "
                "idempotency identity (a repeat with the same key is a no-op)."
            ),
        )

    if not changes:
        return RunReviseResult(
            action="noop",
            preview=False,
            key=key_clean,
            generation=gen,
            current_queue=current_queue,
            changes=[],
            conflicts=conflicts,
            unchanged=unchanged,
            message=(
                "Nothing to change — the requested pipelines already match "
                "the queue. No revision was recorded."
            ),
        )

    # ORCH M4.1: the stop path — only now, when the request actually
    # applies (a live engine + stop_running=True + no conflicts + a key +
    # at least one effective change). The stop goes through the engine's
    # STANDARD kill mechanism (kill_pipeline: TERM → grace → KILL the
    # pipeline, the worker tree kill, the last-kill record — no
    # home-grown signals). The resume point is captured BEFORE the kill —
    # the kill clears the pipeline state (AUD04-08) — and restored after,
    # so a plain ``awf continue`` resumes the unit from the stopped stage.
    resume_from = ""
    if running:
        pre_state = pipeline_state.read_state(project_dir) or {}
        resume_from = str(pre_state.get("stage_name") or "").strip()
        resume_todo = str(pre_state.get("todo_id") or "").strip()

        from .pipeline import kill_pipeline

        kill_pipeline(project_dir)
        # The stop is confirmed by the shared liveness resolver, not by
        # the kill's own report: a process that survived the standard
        # escalation is still ours (the resolver is strict about
        # identity) — the revision must not apply over a live stage.
        still, still_pid, _still_source = _liveness.resolve(project_dir)
        if still:
            return RunReviseResult(
                action="refused",
                preview=False,
                key=key_clean,
                generation=gen,
                current_queue=current_queue,
                changes=changes,
                conflicts=conflicts,
                unchanged=unchanged,
                message=(
                    f"Refused: the stop failed — the stage is still alive "
                    f"(PID {still_pid}). The revision was NOT applied; "
                    "the run state is consistent and the retry is "
                    "possible. The previous plan stays in force."
                ),
            )
        if resume_from or resume_todo:
            restore: dict = {}
            if resume_from:
                restore["stage_name"] = resume_from
            if resume_todo:
                restore["todo_id"] = resume_todo
            pipeline_state.write_state(project_dir, **restore)

    # A-13: the queue change and the revision record are ONE CAS write
    # conditioned on (generation, index, current) — the same pattern as the
    # run_next reservation/commit. The idempotency check (find_revision)
    # runs under the same lock hold: two serialized writers cannot race
    # into a second revision.
    outcome: dict = {}

    def _revise_mutator(st: dict) -> dict:
        existing = run_state.find_revision(st, key_clean)
        if existing is not None:
            outcome["existing"] = existing
            return st
        for ch in changes:
            for q in st.get("queue") or []:
                if isinstance(q, dict) and str(q.get("todo_id", "")) == ch["todo_id"]:
                    q["pipeline"] = ch["to"]
        # ORCH M6.3 (seam: binding×revision): a revision changes the run's
        # PLAN — the queue the run continues with is new, so the run moves
        # to a new cycle (the same bump a force-restart gets in run_start).
        # Without it, an approval published for the pre-revision
        # generation (M2.1 binding) survives the revision and unlocks the
        # commit the revision was meant to invalidate: binding_refusal
        # compares the binding's generation with the CURRENT one and
        # documents the revised-run refusal, but nothing ever moved the
        # current generation. The bump and the queue change are one CAS
        # write; the revision record keeps `gen` — the generation the
        # revision was applied AGAINST (the CAS identity).
        st["generation"] = run_state.generation_of(st) + 1
        run_state.append_revision(
            st, key_clean, reason_clean, changes, gen, resume_from=resume_from
        )
        outcome["applied"] = True
        return st

    _written, cas_ok = run_state.update_run_cas(
        project_dir,
        _revise_mutator,
        generation=gen,
        index=index,
        current=current,
    )
    if not cas_ok:
        fresh = run_state.read_run(project_dir) or {}
        fresh_queue, _c, _f, _u = _revision_items(fresh, requested)
        return RunReviseResult(
            action="refused",
            preview=False,
            key=key_clean,
            generation=run_state.generation_of(fresh),
            current_queue=fresh_queue,
            changes=changes,
            conflicts=conflicts,
            unchanged=unchanged,
            message=(
                "Refused: the run changed between the read and the write "
                "(generation/index/current mismatch — A-13 CAS). The "
                "previous plan stays in force; the current state is in "
                "current_queue/generation."
            ),
        )

    if "existing" in outcome:
        stored = outcome["existing"]
        return RunReviseResult(
            action="noop",
            preview=False,
            key=key_clean,
            generation=gen,
            current_queue=current_queue,
            changes=[],
            conflicts=conflicts,
            unchanged=unchanged,
            revision=stored,
            message=(
                f"Revision with key {key_clean} was already applied "
                f"({str(stored.get('ts') or '?')}) — no-op, nothing was "
                "written twice."
            ),
        )

    # _written is the state the CAS just wrote — its last revision entry is
    # exactly the record this call appended (no re-read).
    written_revisions = (_written or {}).get("revisions") or []
    entry = written_revisions[-1] if written_revisions else None
    resume_note = ""
    if resume_from:
        resume_note = (
            f" The stopped unit: continue will resume from stage "
            f"'{resume_from}' (awf continue)."
        )
    return RunReviseResult(
        action="applied",
        preview=False,
        key=key_clean,
        generation=gen,
        current_queue=[dict(e) for e in current_queue],
        changes=changes,
        conflicts=conflicts,
        unchanged=unchanged,
        revision=entry,
        resume_from=resume_from,
        message=(
            f"Queue revised (key {key_clean}): {len(changes)} element(s) "
            f"changed — the pipelines take effect at each element's launch "
            f"(awf_run_next).{resume_note}"
        ),
        next_action=(
            f"Resume the stopped unit with awf_continue (it continues from "
            f"stage '{resume_from}'), then awf_run_next for the next "
            "element."
            if resume_from
            else "Continue the run with awf_run_next."
        ),
    )


# ─── ORCH M5.2: service run (служебный забег создания роли) ─────────────
#
# A service run is a FULL run — queue, position, verdicts, report — in its
# OWN state file (state/service-run.yaml, slot="service"): the supervisor
# creates a role candidate DURING the main run without disturbing it. The
# main run.yaml is never opened for writing by the service flow (the file
# is the unit of atomicity — that is what makes the main run survive the
# service run byte-for-byte, crash included). The unit runs the SAME
# engine: the generated pipeline (a snapshot of the active composition)
# declares the draft candidate as the worker stage's output (M3.2
# freshness check) and verify never commits (the candidate stays in the
# draft area — adopt is a separate explicit step, M5.1).


#: Prefix of awf-GENERATED service pipeline files (role-draft-<slug>).
#: Such a file is managed by run_service_start (regenerated on every
#: start, never hand-edited); user pipelines keep other names.
SERVICE_PIPELINE_PREFIX = "role-draft-"


def _service_pipeline_name(slug: str) -> str:
    return f"{SERVICE_PIPELINE_PREFIX}{slug}"


def _draft_candidate_rel_path(slug: str) -> str:
    """The service unit's declared output: the draft candidate path
    (project-relative, M3.2)."""
    return f".agentic/roles/draft/{slug}.md"


def _service_todo_content(slug: str, description: str) -> str:
    cand = _draft_candidate_rel_path(slug)
    return (
        f"# TODO — создать роль-кандидат `{slug}` (служебный забег, ORCH M5.2)\n"
        "\n"
        "Создай роль-кандидата в draft-области (ORCH M5.1):\n"
        f"- путь кандидата (declared output стадии, движок проверит): `{cand}`\n"
        "- содержимое — роль по описанию ниже: front-matter "
        "(name/description) + инструкции роли, как у живых ролей в "
        ".agentic/roles/.\n"
        "\n"
        "Описание роли (от супервизора):\n"
        f"{str(description).strip()}\n"
        "\n"
        "Запреты: НЕ коммитить (кандидат остаётся в draft до adopt, M5.1); "
        "не трогать живые роли; не расширять scope за пределы кандидата.\n"
    )


def _service_slug_of(state: dict) -> str:
    """The slug of a service run state (derived from the unit's pipeline
    name role-draft-<slug>; "" when the state carries no such item)."""
    for q in state.get("queue") or []:
        pipe = str(q.get("pipeline", "") or "") if isinstance(q, dict) else ""
        if pipe.startswith(SERVICE_PIPELINE_PREFIX):
            return pipe[len(SERVICE_PIPELINE_PREFIX):]
    return ""


def _write_service_pipeline(project_dir: Path, slug: str) -> Path:
    """ORCH M5.2: generate the service pipeline file.

    A snapshot of the ACTIVE composition with the service deltas:
    - the FIRST non-supervisor stage declares the draft candidate as its
      output (``output: .agentic/roles/draft/<slug>.md``) — the engine
      verifies exists+fresh before the stage advances (M3.2);
    - non-supervisor stages are ``on_blocked: stop`` — a blocked service
      unit stops the pipeline instead of replanning (a replan would
      re-plan a NEW TODO and break the service queue's pinning; the unit
      is disposable: retry = a fresh service run);
    - the verify stage is ``on_approved: next`` — the service run NEVER
      commits: the commit gate is bound to the MAIN run's generation, and
      the candidate stays in the draft area until an explicit adopt
      (M5.1).

    The file is awf-managed (the role-draft- prefix, a marker header): it
    is regenerated on every start and never overwrites a pipeline under
    another name. The document is validated with the same schema as a
    hand-written pipeline (A-06) before it is written.
    """
    import yaml

    from ..pipeline import (
        load_stages,
        resolve_pipeline_file,
        validate_pipeline_document,
    )

    source_file = resolve_pipeline_file(project_dir)
    stages = load_stages(source_file)
    if not stages:
        raise AwfApiError(
            f"cannot generate the service pipeline: the active pipeline "
            f"{source_file.name} loads empty (broken YAML?) — fix it first."
        )
    worker_stage = next(
        (s for s in stages if s.role.strip().lower() != "supervisor"), None
    )
    if worker_stage is None:
        raise AwfApiError(
            "cannot generate the service pipeline: the active pipeline has "
            "no non-supervisor stage that could write the role candidate — "
            "a service run needs a worker stage."
        )
    doc_stages: list[dict] = []
    for st in stages:
        entry: dict = {"name": st.name, "role": st.role}
        if st.description:
            entry["description"] = st.description
        if st.id:
            entry["id"] = st.id
        if st.task:
            entry["task"] = st.task
        if st.input:
            entry["input"] = st.input
        if st.output:
            entry["output"] = st.output
        entry["on_blocked"] = st.on_blocked
        entry["on_approved"] = st.on_approved
        entry["on_rejected"] = st.on_rejected
        entry["on_failed"] = st.on_failed
        entry["max_retries"] = st.max_retries
        entry["max_rollbacks"] = st.max_rollbacks
        is_worker = st.role.strip().lower() != "supervisor"
        if is_worker:
            entry["on_blocked"] = "stop"
        if st is worker_stage:
            cand = _draft_candidate_rel_path(slug)
            # The unit's contract (TODO-0144 invariant 3): the candidate
            # IS the declared output — a source-declared output on this
            # stage is replaced by it. The task is set too: the "Stage
            # assignment" block (with the "Expected output:" line the
            # worker must honor) only renders when the stage has a task
            # (agent_stage._stage_assignment_block).
            entry["task"] = (
                f"Создай роль-кандидат `{slug}` по описанию из TODO и "
                f"запиши файл в declared output `{cand}`."
            )
            entry["output"] = cand
        if st.kind == "verify" and entry["on_approved"] != "next":
            entry["on_approved"] = "next"
        doc_stages.append(entry)
    name = _service_pipeline_name(slug)
    document = {"name": name, "stages": doc_stages}
    validate_pipeline_document(document, name)
    header = (
        f"# {name}.yaml — ORCH M5.2 service-run pipeline "
        f"(awf-generated, role draft)\n"
        f"# source: pipelines/{source_file.name}.yaml\n"
        f"# slug: {slug}\n"
        f"# generated: {run_state.now_iso()}\n"
    )
    out = paths.agentic_dir(project_dir) / "pipelines" / f"{name}.yaml"
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(out, header + yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    return out


def _rollback_service_side_effects(
    project_dir: Path,
    todo_id: str,
    pipe_name: str,
    *,
    clear_slot: bool = True,
) -> None:
    """V-01/V-02: undo EVERY side effect of a failed service start — the
    service state file, the unit's TODO (md + .ready + baseline files) and
    the generated pipeline file. The main run.yaml is a different file the
    service flow never wrote, so there is nothing to restore there.
    Best-effort: a cleanup failure is not raised (the refusal still
    stands).

    ``clear_slot`` (review fix F2): the service state file is cleared only
    when it belongs to THIS call. On the conflict path the slot holds the
    concurrent WINNER's state (the loser's mutator saw active=True and
    returned it unchanged) — clearing it would orphan the winner's live
    pipeline with no run record. The reference is run_start's conflict
    path (just raise; the winner's state stands), so the conflict call
    passes clear_slot=False and undoes only its own files."""
    if clear_slot:
        run_state.clear_run(project_dir, slot="service")
    inbox = paths.inbox(project_dir)
    for suffix in (".md", ".ready"):
        try:
            (inbox / f"{todo_id}{suffix}").unlink()
        except OSError:
            pass
    ctx = paths.context_dir(project_dir)
    for name in (
        f"BASELINE-{todo_id}.sha",
        f"BASELINE-{todo_id}.status",
        f"BASELINE-{todo_id}.tests.log",
        f"BASELINE-{todo_id}.env.log",
        f"BASELINE-{todo_id}.untracked",
        f"BASELINE-{todo_id}.include",
        f"BASELINE-{todo_id}.carry_over",
    ):
        try:
            (ctx / name).unlink()
        except OSError:
            pass
    try:
        (paths.agentic_dir(project_dir) / "pipelines" / f"{pipe_name}.yaml").unlink()
    except OSError:
        pass


def run_service_start(
    project_dir: Path,
    *,
    slug: str,
    description: str,
    background: bool = True,
) -> ServiceRunResult:
    """ORCH M5.2: launch a SERVICE run — role creation during the main run.

    The service run is its own run in a SEPARATE state file
    (``state/service-run.yaml``): a queue of ONE unit («создай роль
    <slug> по описанию …»). The main ``state/run.yaml`` is not replaced
    and not finished — after the service run completes or fails the main
    run is exactly as it was (byte-level invariant, tested).

    The unit runs the SAME engine: the generated pipeline (a snapshot of
    the active composition; the first non-supervisor stage declares the
    draft candidate as its output, M3.2; verify never commits) is launched
    via the standard ``start_pipeline`` with the service TODO pinned.
    One pipeline per project (the engine's guarantee): while any stage is
    live the launch is refused BEFORE side effects — stop the unit first
    (awf_kill) or wait for the stage (M4.1 stop semantics).

    A failed launch rolls back every side effect of this call (V-01/V-02):
    no service state, no unit TODO, no pipeline file; the main run is
    untouched either way.
    """
    project_dir = _require_run_project(project_dir)
    from .roles import _validate_draft_slug

    slug_clean = _validate_draft_slug(slug)
    description_clean = str(description or "").strip()
    if not description_clean:
        raise AwfApiError(
            "description is required — the role creation task text "
            "(what the role does)."
        )

    main_state = run_state.read_run(project_dir)
    if not main_state or not main_state.get("active"):
        raise AwfApiError(
            "No active main run — a service run exists to create a role "
            "DURING a run without disturbing it. Start the main run first "
            "(awf_run_start), or create the role directly (awf_add_role "
            "with draft=true)."
        )
    existing = run_state.read_run(project_dir, slot="service")
    if existing and existing.get("active"):
        ex_cur = str(
            existing.get("current")
            or (existing.get("queue") or [{}])[0].get("todo_id", "")
        )
        raise AwfApiError(
            f"A service run is already active (unit {ex_cur}). Finish it "
            "(awf_run_service_finish) or stop its pipeline (awf_kill) "
            "first — one service run at a time."
        )

    # The engine's single-pipeline guarantee: a live stage (usually the
    # main unit's) refuses the launch BEFORE any side effect.
    running, pid, _source = _liveness.resolve(project_dir)
    if running:
        return ServiceRunResult(
            action="refused",
            slug=slug_clean,
            pipeline=_service_pipeline_name(slug_clean),
            candidate_path=_draft_candidate_rel_path(slug_clean),
            main_active=True,
            main_position=run_state.position(main_state),
            main_current=str(main_state.get("current") or ""),
            message=(
                f"Refused: a pipeline is already running (PID {pid}) — the "
                "engine allows one pipeline per project. Stop the current "
                "unit first (awf_kill) or wait for the stage to finish, "
                "then retry. The main run state was not touched."
            ),
            next_action=(
                "awf_kill (stop the unit) or wait for the stage; then "
                "awf_run_service_start again. The main run continues with "
                "awf_run_next / awf_continue."
            ),
        )

    pipe_name = _service_pipeline_name(slug_clean)
    cand_path = _draft_candidate_rel_path(slug_clean)

    # Side effects — rolled back as a set on launch failure (V-01/V-02).
    _write_service_pipeline(project_dir, slug_clean)
    from .dispatch import dispatch_todo

    dispatch = dispatch_todo(
        project_dir,
        _service_todo_content(slug_clean, description_clean),
        pipeline=pipe_name,
    )
    todo_id = dispatch.todo_id

    outcome: dict = {}

    def _start_mutator(st: dict) -> dict:
        # The service slot's own "inactive → active" check inside the lock
        # (the run_start pattern): a concurrent loser rolls back.
        if st.get("active"):
            outcome["conflict"] = st
            return st
        new_state = dict(st)
        new_state.update(
            active=True,
            queue=[{"todo_id": todo_id, "pipeline": pipe_name}],
            index=0,
            current="",
            completed=[],
            rejects={},
            outcomes={},
            stop_flags={},
            budget_minutes=0,
            downtime_seconds=0,
            started_at=run_state.now_iso(),
            generation=run_state.generation_of(st) + 1,
            stop_reason="",
            report_file="",
            note=f"service run: role candidate {slug_clean}",
            no_checkpoints=True,
            goal=f"service: create role candidate {slug_clean} (draft)",
            criteria=[],
            decisions=[],
            evidence_plans=[],
        )
        return new_state

    run_state.update_run(project_dir, _start_mutator, slot="service")
    if "conflict" in outcome:
        # F2: the slot now holds the concurrent WINNER's state (ownership
        # CAS — the loser's mutator saw active=True, so the file no longer
        # carries this call's todo_id). Undo this call's files only; the
        # winner's run record stands (run_start's conflict reference).
        _rollback_service_side_effects(
            project_dir, todo_id, pipe_name, clear_slot=False
        )
        raise AwfApiError(
            "A service run was started concurrently — this call rolled "
            "back its own side effects (the other run's state was kept). "
            "Check awf_run_service_status."
        )

    from .pipeline import start_pipeline

    launch_failed = True
    refusal_message = ""
    run_mode = ""
    run_id: int | None = None
    log_file = ""
    try:
        result = start_pipeline(
            project_dir,
            background=background,
            pipeline=pipe_name,
            todo_id=todo_id,
            # The service launch is its own process: the BD-36 checkpoint
            # is skipped for it (the main run's no_checkpoints flag is the
            # main's; the owner does not sit at a service run's checkpoint).
            no_checkpoints=True,
        )
        run_mode = result.run_mode
        run_id = result.run_id
        log_file = result.log_file or ""
        launch_failed = result.run_mode in ("noop", "error") or (
            result.run_mode == "foreground" and (result.exit_code or 0) != 0
        )
        refusal_message = f"Launch failed: {result.message}"
    except Exception as e:
        # V-02: an exception in the launch window is a classified refusal
        # that rolls back this call's own effects.
        refusal_message = (
            f"Launch failed: start_pipeline raised {type(e).__name__}: {e}"
        )

    if launch_failed:
        _rollback_service_side_effects(project_dir, todo_id, pipe_name)
        return ServiceRunResult(
            action="refused",
            slug=slug_clean,
            pipeline=pipe_name,
            candidate_path=cand_path,
            main_active=True,
            main_position=run_state.position(main_state),
            main_current=str(main_state.get("current") or ""),
            message=refusal_message,
            next_action=(
                "Investigate the pipeline state (awf_status), then retry "
                "awf_run_service_start. The main run is untouched."
            ),
        )

    # The position commit AFTER a successful launch (the run_next pattern):
    # if the run closed inside the launch window, nothing is recorded —
    # the launched pipeline is orphaned (accepted consequence, said in the
    # result), never silently swallowed.
    applied: dict = {}

    def _advance_mutator(st: dict) -> dict:
        if not st.get("active"):
            return st  # closed during the window — veto, nothing recorded
        st["index"] = 1
        st["current"] = todo_id
        todo_md = paths.inbox(project_dir) / f"{todo_id}.md"
        run_state.append_evidence_plan(st, todo_id, _todo_evidence_plan(todo_md))
        applied["ok"] = True
        return st

    run_state.update_run(project_dir, _advance_mutator, slot="service")

    if not applied.get("ok"):
        return ServiceRunResult(
            action="started",
            todo_id=todo_id,
            slug=slug_clean,
            pipeline=pipe_name,
            candidate_path=cand_path,
            run_mode=run_mode,
            run_id=run_id,
            log_file=log_file,
            main_active=True,
            main_position=run_state.position(main_state),
            main_current=str(main_state.get("current") or ""),
            message=(
                f"Service unit {todo_id} launched ({run_mode}) — but the "
                "service run was closed during the launch window, so the "
                "position was NOT recorded. The launched pipeline is "
                "orphaned: inspect it with awf_status and decide "
                "(awf_kill + a fresh service run, or take it over)."
            ),
            next_action="Inspect the orphaned pipeline with awf_status, then decide.",
        )

    return ServiceRunResult(
        action="started",
        todo_id=todo_id,
        slug=slug_clean,
        pipeline=pipe_name,
        candidate_path=cand_path,
        run_mode=run_mode,
        run_id=run_id,
        log_file=log_file,
        main_active=True,
        main_position=run_state.position(main_state),
        main_current=str(main_state.get("current") or ""),
        message=(
            f"Service run started: unit {todo_id} (role {slug_clean}, "
            f"pipeline {pipe_name}) launched ({run_mode}). Main run "
            f"untouched (active, position {run_state.position(main_state)}). "
            f"Candidate: {cand_path}."
        ),
        next_action=(
            "GO IDLE — wait for the verify event (awf_wait_for_event). On "
            "verify: run your probes, then awf_run_service_approve("
            "evidence=...) for the service unit. Close the service run "
            "with awf_run_service_finish; the main run continues with "
            "awf_run_next / awf_continue."
        ),
    )


def _write_service_report(
    project_dir: Path, svc: dict, reason: str
) -> Path | None:
    """SERVICE-RUN-REPORT-{ts}.md in the outbox — the service run's OWN
    report (never a RUN-REPORT: those belong to the main run). It always
    carries the main run's snapshot — the proof that the service run did
    not touch it — and the continuation command."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outbox = paths.outbox(project_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    report = outbox / f"SERVICE-RUN-REPORT-{ts}.md"
    slug = _service_slug_of(svc)
    queue = list(svc.get("queue") or [])
    completed = [str(c) for c in (svc.get("completed") or [])]
    rejects = svc.get("rejects") or {}
    main_state = run_state.read_run(project_dir)
    main_pos = run_state.position(main_state) if main_state else "—"
    main_cur = str((main_state or {}).get("current") or "")
    main_active = bool(main_state and main_state.get("active"))
    lines = [
        f"# Service run report ({ts})",
        "",
        f"**Reason:** {reason}",
        f"**Role:** {slug or '—'}"
        + (f" (candidate: {_draft_candidate_rel_path(slug)})" if slug else ""),
        f"**Queue:** {', '.join(_queue_item_label(q) for q in queue) or '—'}"
        f" — {len(completed)}/{len(queue)} done",
        f"**Current at stop:** {str(svc.get('current') or '—')}",
        f"**Rejects:** {', '.join(f'{k}×{v}' for k, v in rejects.items()) or '—'}",
        f"**Elapsed:** {int(run_state.elapsed_minutes(svc))} min",
        "",
        "## Main run (untouched)",
        "",
        f"- active: {main_active}",
        f"- position: {main_pos}",
        f"- current: {main_cur or '—'}",
        "",
        "Continue the main run: awf_run_next (launches the next queued "
        "unit) or awf_continue (resumes a stopped unit from its stage).",
        "",
    ]
    try:
        atomic_write_text(report, "\n".join(lines))
    except OSError:
        return None
    return report


def run_service_finish(
    project_dir: Path,
    *,
    reason: str = "service unit processed",
) -> ServiceRunResult:
    """ORCH M5.2: close the service run — the SERVICE-RUN-REPORT goes to
    the outbox, the service slot is marked inactive, and the answer names
    the main continuation («продолжай основной: awf_run_next /
    awf_continue») with the main run's snapshot.

    Refused while a pipeline is still live (the service unit is in
    flight): wait for verify/salvage or awf_kill first (M4.1 stop
    semantics — this call never kills). Idempotent: an already-closed
    (or absent) service run is a no-op. The main run.yaml is never
    written by this flow.
    """
    project_dir = _require_run_project(project_dir)
    svc = run_state.read_run(project_dir, slot="service")
    slug = _service_slug_of(svc) if svc else ""
    pipe_name = _service_pipeline_name(slug) if slug else ""
    cand_path = _draft_candidate_rel_path(slug) if slug else ""
    if not svc:
        return ServiceRunResult(
            action="noop",
            slug=slug,
            message="No service run — nothing to finish.",
        )
    if not svc.get("active"):
        return ServiceRunResult(
            action="noop",
            slug=slug,
            pipeline=pipe_name,
            candidate_path=cand_path,
            message=(
                "Service run already closed"
                + (f" ({svc.get('stop_reason')})" if svc.get("stop_reason") else "")
                + "."
            ),
        )

    # M4.1 stop semantics: a live stage is refused (stop it explicitly),
    # not killed here.
    running, pid, _source = _liveness.resolve(project_dir)
    if running:
        return ServiceRunResult(
            action="refused",
            slug=slug,
            pipeline=pipe_name,
            candidate_path=cand_path,
            message=(
                f"Refused: a pipeline is still running (PID {pid}) — the "
                "service unit is in flight. Wait for verify/salvage "
                "(awf_wait_for_event), or awf_kill first, then retry. "
                "The main run is untouched."
            ),
            next_action="Wait for the verify event or awf_kill, then retry awf_run_service_finish.",
        )

    report = _write_service_report(project_dir, svc, reason)

    def _close_mutator(st: dict) -> dict:
        st["active"] = False
        st["stop_reason"] = reason
        st["report_file"] = str(report) if report else ""
        return st

    run_state.update_run(project_dir, _close_mutator, slot="service")

    main_state = run_state.read_run(project_dir)
    main_pos = run_state.position(main_state) if main_state else "—"
    main_cur = str((main_state or {}).get("current") or "")
    return ServiceRunResult(
        action="stopped",
        slug=slug,
        pipeline=pipe_name,
        candidate_path=cand_path,
        report_file=str(report) if report else "",
        main_active=bool(main_state and main_state.get("active")),
        main_position=main_pos,
        main_current=main_cur,
        message=(
            f"Service run stopped: {reason}. Main run untouched (active: "
            f"{bool(main_state and main_state.get('active'))}, position "
            f"{main_pos}, current {main_cur or '—'})."
        ),
        next_action=(
            "Continue the main run: awf_run_next(project_dir) launches the "
            "next queued unit; awf_continue(project_dir) resumes the main "
            "unit from its stopped stage (when one was stopped)."
        ),
    )


def run_service_status(project_dir: Path) -> ServiceRunStatusResult:
    """ORCH M5.2: one read, two snapshots — the service run (unit, slug,
    pipeline, candidate path, position, close reason) and the main run
    (position, current). Read-only; degrades to "no service run" on a
    corrupt service file — with a ``warning`` naming the unreadable file,
    since the file exists (the main run is unaffected)."""
    project_dir = _require_run_project(project_dir)
    svc = run_state.read_run(project_dir, slot="service")
    main_state = run_state.read_run(project_dir)
    # ORCH M6.4: a state file that EXISTS but cannot be read is corrupt
    # state, not "never started" — degrade to no service run and say so
    # (the run.yaml precedent: run_plan_read.read_run_record's warning).
    # The file is kept for inspection.
    warning = ""
    if svc is None and run_state.run_file(project_dir, slot="service").is_file():
        warning = (
            "service-run.yaml is unreadable (broken YAML or invalid shape) — "
            "treated as no service run; the file is kept at "
            ".agentic/state/service-run.yaml"
        )
    slug = _service_slug_of(svc) if svc else ""
    svc_queue = list((svc or {}).get("queue") or [])
    todo_id = ""
    pipeline = ""
    if svc_queue and isinstance(svc_queue[0], dict):
        todo_id = str(svc_queue[0].get("todo_id", ""))
        pipeline = str(svc_queue[0].get("pipeline", "") or "")
    service_active = bool(svc and svc.get("active"))
    if svc:
        message = (
            f"Service run: {'active' if service_active else 'closed'}, "
            f"position {run_state.position(svc)}, unit {todo_id or '—'} "
            f"(role {slug or '—'}), candidate "
            f"{_draft_candidate_rel_path(slug) if slug else '—'}."
        )
        if not service_active and svc.get("stop_reason"):
            message += f" Closed: {svc.get('stop_reason')}."
    elif warning:
        message = warning
    else:
        message = "No service run."
    main_pos = run_state.position(main_state) if main_state else "—"
    return ServiceRunStatusResult(
        service_active=service_active,
        message=message,
        warning=warning,
        todo_id=todo_id,
        slug=slug,
        pipeline=pipeline,
        candidate_path=_draft_candidate_rel_path(slug) if slug else "",
        position=run_state.position(svc) if svc else "0/0",
        index=int((svc or {}).get("index", 0) or 0),
        current=str((svc or {}).get("current") or ""),
        completed=[str(c) for c in ((svc or {}).get("completed") or [])],
        stop_reason=str((svc or {}).get("stop_reason") or ""),
        report_file=str((svc or {}).get("report_file") or ""),
        main_active=bool(main_state and main_state.get("active")),
        main_position=main_pos,
        main_current=str((main_state or {}).get("current") or ""),
        next_action=(
            "Wait for the verify event (awf_wait_for_event), then "
            "awf_run_service_approve(evidence=...) and "
            "awf_run_service_finish; the main run continues with "
            "awf_run_next / awf_continue."
            if service_active
            else ""
        ),
    )


def run_service_approve(
    project_dir: Path,
    todo_id: str,
    *,
    evidence: str,
) -> ServiceRunApproveResult:
    """ORCH M5.2: approve the service unit at its verify stage.

    Publishes the signal pair the engine's verify wait accepts in run
    mode: ``context/RUN-EVIDENCE-{todo}.md`` (the supervisor's
    independent-verification trace — the engine's evidence gate, which is
    ON while the MAIN run is active) and ``inbox/APPROVE-{todo}.ready``
    (an EMPTY marker — the service pipeline never commits, so there is no
    M2.1 binding to stamp and no verified_sha to record).

    The verdict is recorded in the SERVICE slot only (outcomes + the
    causal decision) — the main run's diary is never touched. Only the
    CURRENT unit of an active service run can be approved, and evidence
    is required (a service unit is a run unit).
    """
    project_dir = _require_run_project(project_dir)
    if not re.match(r"^TODO-\d{4,}$", str(todo_id or "")):
        raise AwfApiError(f"invalid todo_id '{todo_id}', expected format TODO-NNNN")
    evidence_clean = str(evidence or "").strip()
    if not evidence_clean:
        raise AwfApiError(
            "evidence is required: the commands you actually ran and the "
            "verdict, e.g. evidence='pytest -q → 3 passed; verdict: "
            "approve'."
        )
    svc = run_state.read_run(project_dir, slot="service")
    if not svc or not svc.get("active"):
        raise AwfApiError("No active service run — nothing to approve.")
    if str(svc.get("current") or "") != todo_id:
        raise AwfApiError(
            f"{todo_id} is not the current unit of the service run "
            f"(current: {svc.get('current') or '—'}) — a service approve "
            "approves only the service unit at its verify stage."
        )

    ctx = paths.context_dir(project_dir)
    ctx.mkdir(parents=True, exist_ok=True)
    evidence_file = ctx / f"RUN-EVIDENCE-{todo_id}.md"

    # Verdict first, signal second (the A-04 pattern): a counted reject
    # must refuse WITHOUT the file — a leftover APPROVE would unlock the
    # engine's wait on a rejected verdict.
    conflict = {"no": True}

    def _mut(state: dict) -> dict:
        if not state.get("active"):
            return state
        rejects_map = state.get("rejects") or {}
        if int(rejects_map.get(todo_id, 0) or 0) >= 1:
            conflict["no"] = False
            return state
        outcomes = dict(state.get("outcomes") or {})
        outcomes[todo_id] = {"verdict": "approved"}
        state["outcomes"] = outcomes
        run_state.append_decision(state, "approve", todo_id, evidence_clean[:200])
        return state

    run_state.update_run(project_dir, _mut, slot="service")
    if not conflict["no"]:
        raise AwfApiError(
            f"{todo_id}: a rejection already counted for this service unit "
            "— the verdict stays 'rejected'. Resolve it (a fresh service "
            "run) before approving."
        )
    atomic_write_text(
        evidence_file,
        f"# Service run evidence — {todo_id}\n\n"
        f"_Recorded: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}_\n\n"
        f"{evidence_clean}\n",
    )
    inbox = paths.inbox(project_dir)
    inbox.mkdir(parents=True, exist_ok=True)
    signal = inbox / f"APPROVE-{todo_id}.ready"
    signal.touch()
    return ServiceRunApproveResult(
        todo_id=todo_id,
        signal_file=str(signal),
        evidence_file=str(evidence_file),
        message=(
            f"{todo_id}: service unit approved — the verdict is recorded "
            "in the service slot (the main run diary is untouched)."
        ),
    )
