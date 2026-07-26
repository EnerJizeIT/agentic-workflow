"""Port of lib/rollback.sh — ``awf rollback`` command."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import paths


def run(args: Any) -> int:
    """Execute ``awf rollback`` and return exit code."""
    todo_id = args.todo_id
    mode = "hard"
    if getattr(args, "soft", False):
        mode = "soft"
    if getattr(args, "dry_run", False):
        mode = "dry-run"

    context_dir = paths.context_dir(".")
    inbox = paths.inbox(".")

    baseline_sha_file = context_dir / f"BASELINE-{todo_id}.sha"
    if not baseline_sha_file.exists():
        print(f"ERROR: Baseline SHA not found: {baseline_sha_file}")
        print("Cannot rollback without baseline.")
        return 1

    baseline_sha = baseline_sha_file.read_text(encoding="utf-8").strip()

    print(f"Rolling back {todo_id} to baseline: {baseline_sha}")
    print(f"Mode: {mode}")

    if mode == "dry-run":
        print(f"[DRY RUN] Would run: git reset --{mode[5:]} {baseline_sha}")
        print()
        print("Changes since baseline:")
        import subprocess
        result = subprocess.run(
            ["git", "diff", baseline_sha, "--stat"],
            capture_output=True, text=True, check=False,
        )
        print(result.stdout)
        return 0

    import subprocess
    if mode == "hard":
        subprocess.run(["git", "reset", "--hard", baseline_sha], check=True)
    else:
        subprocess.run(["git", "reset", baseline_sha], check=True)

    # Create ACK file
    inbox.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ack_content = f"""signal: TASK_ACK
ack_type: DONE
decision: rollback
referenced_task_id: {todo_id}
baseline_sha: {baseline_sha}
created_by: supervisor
created_at: {ts}
"""
    (inbox / f"ACK-{todo_id}.ready").write_text(ack_content, encoding="utf-8")

    print(f"Rolled back to {baseline_sha}")
    print(f"ACK file: {inbox}/ACK-{todo_id}.ready")
    return 0
