"""Port of lib/report.sh — ``awf report`` command."""
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config as cfg_mod
from . import paths


def run(args: Any) -> int:
    """Execute ``awf report`` and return exit code."""
    agentic = Path(".agentic")
    if not agentic.is_dir():
        print("No .agentic/ found. Run 'awf init' first.")
        return 1

    inbox = paths.inbox(".")
    outbox = paths.outbox(".")
    config_file = paths.config_file(".")

    project_name = "Project"
    if config_file.exists():
        config_data = cfg_mod.load(".")
        name = cfg_mod.get(config_data, "project.name", "Project") or "Project"
        project_name = name

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("==============================================")
    print("  Agentic Workflow Report")
    print(f"  Project: {project_name}")
    print(f"  Generated: {now}")
    print("==============================================")
    print()

    done_count = 0
    blocked_count = 0

    if inbox.exists():
        for ready_file in sorted(inbox.glob("TODO-*.ready")):
            if not ready_file.is_file():
                continue
            todo_id = ready_file.stem

            step = "unknown"
            try:
                content = ready_file.read_text(encoding="utf-8")
                for line in content.splitlines():
                    if "step:" in line:
                        step = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass

            if (outbox / f"DONE-{todo_id}.ready").exists():
                print(f"  OK   {todo_id}  {step}")
                done_count += 1
            elif (outbox / f"BLOCKED-{todo_id}.ready").exists():
                print(f"  BLK  {todo_id}  {step}")
                blocked_count += 1
            else:
                print(f"  ...  {todo_id}  {step}  (in progress)")

    print()
    print(f"Completed: {done_count} | Blocked: {blocked_count}")

    # Git diff stats
    print()
    print("Files changed:")
    result = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        print(result.stdout)
    else:
        print("  (no changes or not a git repo)")

    # Latest test results
    if outbox.exists():
        logs = sorted(outbox.glob("TEST-RESULTS-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if logs:
            latest = logs[0]
            try:
                content = latest.read_text(encoding="utf-8")
                lines = content.splitlines()
                tail = lines[-5:] if len(lines) > 5 else lines
                print()
                print("Latest test results:")
                for line in tail:
                    print(line)
            except OSError:
                pass

    return 0
