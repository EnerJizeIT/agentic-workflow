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


def _validate_queue(queue: list[str] | None) -> list[str]:
    ids = [str(q).strip() for q in (queue or []) if str(q).strip()]
    if not ids:
        raise AwfApiError(
            "queue is required — a non-empty list of TODO ids, e.g. ['TODO-0010', 'TODO-0011']"
        )
    for q in ids:
        if not _TODO_RE.match(q):
            raise AwfApiError(f"invalid TODO id {q!r} in queue — expected TODO-NNNN")
    if len(ids) != len(set(ids)):
        raise AwfApiError("queue contains duplicate TODO ids")
    return ids


def run_brief(project_dir: Path) -> dict | None:
    """Compact run state for status/dashboard. None when no run state exists."""
    state = run_state.read_run(project_dir)
    if not state:
        return None
    budget = int(state.get("budget_minutes", 0) or 0)
    elapsed = run_state.elapsed_minutes(state)
    left = max(0, int(budget - elapsed)) if budget else 0
    return {
        "active": bool(state.get("active")),
        "position": run_state.position(state),
        "note": str(state.get("note") or ""),
        "current": state.get("current", ""),
        "completed": list(state.get("completed") or []),
        "budget_minutes": budget,
        "budget_left_minutes": left,
        "stop_reason": state.get("stop_reason", ""),
        "report_file": state.get("report_file", ""),
    }


def run_start(
    project_dir: Path,
    *,
    queue: list[str] | None = None,
    budget_minutes: int = 0,
    stop_flags: dict[str, list[str]] | None = None,
    note: str = "",
    force: bool = False,
) -> RunStartResult:
    """Start an autonomous run: record the queue and the mechanical gates.

    The queue is a list of TODO ids. The supervisor writes each TODO file
    before calling :func:`run_next` for it; stop flags keyed by TODO id mark
    items awf must never auto-continue past (phase boundaries, external
    audits, owner-decision tasks).
    """
    project_dir = _require_run_project(project_dir)
    ids = _validate_queue(queue)

    existing = run_state.read_run(project_dir)
    if existing and existing.get("active") and not force:
        age = int(run_state.elapsed_minutes(existing))
        raise AwfApiError(
            f"Run already active ({run_state.position(existing)}, current "
            f"{existing.get('current') or '—'}, started {age} min ago). "
            f"Finish it with awf_run_finish, or pass force=true to replace it."
        )

    flags = {str(k): list(v) for k, v in (stop_flags or {}).items() if k}
    replaced = bool(existing and existing.get("active") and force)
    state = run_state.write_run(
        project_dir,
        active=True,
        project_root=str(project_dir),
        queue=ids,
        index=0,
        current="",
        completed=[],
        rejects={},
        outcomes={},
        stop_flags=flags,
        budget_minutes=int(budget_minutes or 0),
        started_at=run_state.now_iso(),
        stop_reason="",
        report_file="",
        note=note.strip(),
    )

    budget_note = f", budget {int(budget_minutes)} min" if budget_minutes else ""
    replace_note = " (previous run replaced)" if replaced else ""
    return RunStartResult(
        active=True,
        queue=ids,
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
    left = max(0, int(budget - elapsed)) if budget else 0
    current = str(state.get("current", "") or "")
    position = run_state.position(state) if state else "0/0"

    if not state:
        message = "No run found — start one with awf_run_start(queue=[...])."
    elif active:
        message = (
            f"Run active: {position}, current {current or '—'}"
            + (f", budget left ~{left} min" if budget else "")
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
        stop_reason=str(state.get("stop_reason", "") or ""),
        report_file=str(state.get("report_file", "") or ""),
        message=message,
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
        f"**Queue:** {', '.join(queue) or '—'} — {len(completed)}/{len(queue)} done",
        f"**Completed:** {', '.join(completed) or '—'}",
        f"**Rejects:** {rejects_note}",
        f"**Salvage events:** {salvage}{health_note}",
        f"**Elapsed:** {int(run_state.elapsed_minutes(state))} min "
        f"(budget {state.get('budget_minutes', 0)} min)",
        "",
    ]
    if outcomes:
        ctx_dir = paths.context_dir(project_dir)
        lines += ["## Run diary (verdicts)", ""]
        for todo, verdict in outcomes.items():
            if isinstance(verdict, dict):
                note = verdict.get("reason") or ""
                extra = ""
                if verdict.get("verdict") == "approved":
                    ev = ctx_dir / f"RUN-EVIDENCE-{todo}.md"
                    if ev.is_file():
                        extra = f" (evidence: {ev.name})"
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
        return stop_run(project_dir, state, "queue exhausted — all items processed")

    budget = int(state.get("budget_minutes", 0) or 0)
    if budget and run_state.elapsed_minutes(state) > budget:
        return stop_run(project_dir, state, f"budget exhausted ({budget} min)")

    next_id = queue[index]

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
        prev = queue[index - 1]
        if not todos.is_archived(project_dir, prev):
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

    # Baseline before the signal (same order as dispatch_todo).
    try:
        from .pipeline import create_baseline

        create_baseline(project_dir, next_id)
    except AwfApiError:
        pass  # best-effort — the orchestrator ensures a baseline at stage start

    (paths.inbox(project_dir) / f"{next_id}.ready").touch()

    from .pipeline import start_pipeline

    result = start_pipeline(
        project_dir,
        background=background,
        from_stage=from_stage,
        auto=auto,
        timeout=timeout,
        todo_id=next_id,  # NEG-2026-09-19 R1: queue order is pinned, not "newest active"
    )

    if result.run_mode in ("noop", "error"):
        # AUD02-02: the launch failed — the run position stays put, so the
        # retry targets the same item instead of skipping it (and the
        # unlaunched TODO is not counted as completed).
        return RunNextResult(
            action="refused",
            todo_id=next_id,
            message=f"Launch failed: {result.message}",
            next_action="Investigate the pipeline state (awf_status), then retry awf_run_next.",
        )

    # AUD02-02: advance the run position only AFTER a successful launch.
    completed = list(state.get("completed") or [])
    if index > 0 and queue[index - 1] not in completed:
        completed.append(queue[index - 1])
    run_state.write_run(
        project_dir, index=index + 1, current=next_id, completed=completed,
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
