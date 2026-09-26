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

import re
from datetime import datetime, timezone
from pathlib import Path

from .. import paths, run_state, todos
from .._atomic import atomic_write_text
from ._errors import AwfApiError
from ._results import (
    RunFinishResult,
    RunNextResult,
    RunStartResult,
    RunStatusResult,
)

_TODO_RE = re.compile(r"^TODO-\d{4,}$")


def _require_run_project(project_dir: Path) -> Path:
    """run_* safety: a real project root has .agentic/ AND config.yaml.

    NEG-2026-09-19 A1: ``run_start`` without ``project_dir`` used the MCP
    subprocess cwd ($HOME), where a bare ``.agentic/`` from old experiments
    existed — a ghost run was recorded there and every later message was
    misleading. Refuse loudly, with the path, before writing anything.
    """
    project_dir = Path(project_dir).resolve()
    agentic = project_dir / ".agentic"
    if not agentic.is_dir():
        raise AwfApiError(
            f"Not an awf project: {project_dir} has no .agentic/ — run 'awf init' first."
        )
    if not (agentic / "config.yaml").is_file():
        raise AwfApiError(
            f"Not an awf project root: {project_dir} (no .agentic/config.yaml). "
            f"Pass project_dir explicitly — the default is the MCP process cwd, "
            f"which is usually not your project."
        )
    return project_dir


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
            if not _TODO_RE.match(todo_id):
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
            if not _TODO_RE.match(s):
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


def _todo_finished(project_dir: Path, todo_id: str) -> bool:
    """AUD05-07 + AUD02-08: a TODO counts as finished when it is archived
    AND not active again — restore_todo leaves done/{id}/ behind, so
    ``is_archived`` alone would let a restored (active again) TODO pass."""
    active_ids = todos.list_active_todos(
        paths.inbox(project_dir), paths.outbox(project_dir)
    )
    return todos.is_archived(project_dir, todo_id) and todo_id not in active_ids


def run_brief(project_dir: Path) -> dict | None:
    """Compact run state for status/dashboard. None when no run state exists."""
    state = run_state.read_run(project_dir)
    if not state:
        return None
    budget = int(state.get("budget_minutes", 0) or 0)
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
        "completed": list(state.get("completed") or []),
        "budget_minutes": budget,
        "budget_left_minutes": left,
        "elapsed_minutes": int(elapsed),
        "downtime_minutes": int(downtime),
        "productive_minutes": int(productive),
        "stop_reason": state.get("stop_reason", ""),
        "report_file": state.get("report_file", ""),
        "no_checkpoints": bool(state.get("no_checkpoints")),
        **pipeline_info,
    }


def run_start(
    project_dir: Path,
    *,
    queue: list[str | dict] | None = None,
    budget_minutes: int = 0,
    stop_flags: dict[str, list[str]] | None = None,
    note: str = "",
    force: bool = False,
    no_checkpoints: bool = False,
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
    """
    project_dir = _require_run_project(project_dir)
    items = _validate_queue(queue)
    ids = [i["todo_id"] for i in items]

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
            # A-13: the run generation — identity of the run for the
            # conditional run_next transitions. A fresh run is always one
            # older than whatever existed (absent = 0).
            generation=run_state.generation_of(st) + 1,
            stop_reason="",
            report_file="",
            note=note.strip(),
            no_checkpoints=bool(no_checkpoints),
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


def run_status(project_dir: Path) -> RunStatusResult:
    """Current run state (or an inactive summary when no run exists)."""
    project_dir = _require_run_project(project_dir)
    state = run_state.read_run(project_dir) or {}
    active = bool(state.get("active"))
    budget = int(state.get("budget_minutes", 0) or 0)
    elapsed = run_state.elapsed_minutes(state) if state else 0.0
    downtime = run_state.downtime_minutes(state) if state else 0.0
    productive = run_state.productive_minutes(state) if state else 0.0
    # B2: the budget is in PRODUCTIVE minutes (elapsed − recorded downtime).
    left = max(0, int(budget - productive)) if budget else 0
    current = str(state.get("current", "") or "")
    position = run_state.position(state) if state else "0/0"

    if not state:
        message = "No run found — start one with awf_run_start(queue=[...])."
    elif active:
        message = (
            f"Run active: {position}, current {current or '—'}"
            + _current_pipeline_hint(state.get("queue"), current)
            + (f", budget left ~{left} min (productive)" if budget else "")
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
        completed=list(state.get("completed") or []),
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


def _write_report(
    project_dir: Path,
    state: dict,
    reason: str,
    summary: str = "",
) -> Path | None:
    """Write RUN-REPORT-{ts}.md to outbox. Returns the path (or None on OSError)."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    outbox = paths.outbox(project_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    report = outbox / f"RUN-REPORT-{ts}.md"

    queue = list(state.get("queue") or [])
    completed = list(state.get("completed") or [])
    rejects = state.get("rejects") or {}
    outcomes = state.get("outcomes") or {}
    rejects_note = ", ".join(f"{k}×{v}" for k, v in rejects.items()) or "—"
    budget = int(state.get("budget_minutes", 0) or 0)
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

    budget = int(state.get("budget_minutes", 0) or 0)
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
    gen = run_state.generation_of(state)
    cur = str(state.get("current", "") or "")

    def _reserve_mutator(st: dict) -> dict:
        st["current"] = next_id
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
        def _release_mutator(st: dict) -> dict:
            st["current"] = cur
            return st

        run_state.update_run_cas(
            project_dir,
            _release_mutator,
            generation=gen,
            index=index,
            current=next_id,
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

    if result.run_mode in ("noop", "error"):
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
            message=f"Launch failed: {result.message}",
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

    return RunNextResult(
        action="started",
        todo_id=next_id,
        message=(
            f"Run item {index + 1}/{len(queue)} launched: {next_id} "
            f"({result.run_mode}). {result.message}"
        ),
        run_mode=result.run_mode,
        run_id=result.run_id,
        log_file=result.log_file or "",
        next_action=(
            "GO IDLE. Wait for the verify event with awf_wait_for_event, then run "
            "your own probes and awf_approve(evidence=...) or awf_reject."
        ),
    )


def run_finish(
    project_dir: Path,
    *,
    reason: str = "finished by supervisor",
    summary: str = "",
) -> RunFinishResult:
    """Close the run: write the report and mark it inactive."""
    project_dir = _require_run_project(project_dir)
    state = run_state.read_run(project_dir)
    if not state:
        return RunFinishResult(
            active=False,
            reason=reason,
            report_file="",
            message="No run state found — nothing to finish.",
        )
    report = _write_report(project_dir, state, reason, summary)
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
