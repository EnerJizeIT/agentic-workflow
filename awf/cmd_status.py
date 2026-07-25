"""Port of lib/status.sh — ``awf status`` command."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths
from . import todos


def _count_done_blocked(inbox: Path, outbox: Path) -> tuple[int, int, list[str]]:
    """Scan TODO-*.ready files and count DONE / BLOCKED (canonical form only).

    Returns (done_count, blocked_count, blocked_ids).
    Mirrors the bash loop in status.sh which only checks canonical form.
    """
    done_count = 0
    blocked_count = 0
    blocked_ids: list[str] = []

    if not inbox.exists():
        return done_count, blocked_count, blocked_ids

    for ready_file in sorted(inbox.glob("TODO-*.ready")):
        if not ready_file.is_file():
            continue
        todo_id = ready_file.stem
        if (outbox / f"DONE-{todo_id}.ready").exists():
            done_count += 1
        elif (outbox / f"BLOCKED-{todo_id}.ready").exists():
            blocked_count += 1
            blocked_ids.append(todo_id)

    return done_count, blocked_count, blocked_ids


def _read_progress(outbox: Path, todo_id: str) -> dict[str, Any]:
    """Parse PROGRESS-TODO-NNNN.md for task counts and last entry."""
    progress_file = outbox / f"PROGRESS-{todo_id}.md"
    if not progress_file.exists():
        return {}

    text = progress_file.read_text(encoding="utf-8")
    task_lines = [line for line in text.splitlines() if line.startswith("## Task")]
    total = len(task_lines)
    done = sum(1 for l in task_lines if "[x]" in l)
    failed = sum(1 for l in task_lines if "[!]" in l)
    last = task_lines[-1] if task_lines else ""

    return {
        "total": total,
        "done": done,
        "failed": failed,
        "last": last,
    }


def _read_ack(inbox: Path, todo_id: str) -> str | None:
    """Read ACK decision from inbox, or None if no ACK file."""
    ack_file = inbox / f"ACK-{todo_id}.ready"
    if not ack_file.exists():
        return None
    text = ack_file.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "decision" in line:
            return line.strip()
    return "none"


def run(args: Any) -> int:
    """Execute ``awf status`` and return exit code."""
    project_dir = Path(args.project_dir).resolve()
    agentic = paths.agentic_dir(project_dir)

    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    inbox = paths.inbox(project_dir)
    outbox = paths.outbox(project_dir)

    # Project name
    config_data = cfg_mod.load(project_dir)
    project_name = cfg_mod.get(config_data, "project.name", "Project") or "Project"

    print("=== Agentic Workflow Status ===")
    print(f"Project: {project_name}")
    print()

    # Active TODOs
    active_ids = todos.list_active_todos(inbox, outbox)
    newest_todo = active_ids[0] if active_ids else ""

    # Count DONE / BLOCKED
    done_count, blocked_count, blocked_ids = _count_done_blocked(inbox, outbox)
    active_count = len(active_ids)

    # Print blocked
    for bid in blocked_ids:
        print(f"Blocked: {bid}")

    # Print active TODOs with progress
    for todo_id in active_ids:
        print(f"Active: {todo_id}")

        ack = _read_ack(inbox, todo_id)
        if ack is not None:
            print(f"  ACK: {ack}")

        progress = _read_progress(outbox, todo_id)
        if progress:
            print(f"  Progress: {progress['done']}/{progress['total']} tasks done")
            if progress["failed"] > 0:
                print(f"  Failed:  {progress['failed']} task(s)")
            if progress["last"]:
                print(f"  Last:    {progress['last']}")
        else:
            print("  Progress: (none — worker has not started)")

    # Summary
    print()
    print("Summary:")
    print(f"  Completed: {done_count}")
    print(f"  Blocked:   {blocked_count}")
    print(f"  Active:    {active_count}")

    # Conflict warning
    if active_count > 1:
        print()
        print(f"\u26a0\ufe0f  {active_count} active TODOs detected.")
        print(f"   Orchestrator will run: {newest_todo} (highest NNNN).")
        print("   The other(s) are stale and should be closed explicitly:")
        print("     - rollback:    awf rollback TODO-NNNN")
        print("     - drop orphan: awf reset --orphans")

    # All clear
    if active_count == 0 and blocked_count == 0:
        print()
        print("No active tasks. Supervisor should create the next TODO, then run: awf start")

    return 0
