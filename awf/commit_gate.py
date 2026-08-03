"""BD-8/17: commit gate — auto-commit with optional APPROVE/ACK signal wait.

Extracted from orchestrator.py (A6 refactor).

A1 fix: commit only files changed since baseline (not `git add -A`).
Supervisor's mid-flight edits stay out of worker commits.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from . import git_utils, paths
from ._log import log as _log

APPROVE_TIMEOUT_SECONDS: int = int(os.environ.get("AWF_APPROVE_TIMEOUT_SECONDS", "1800"))
APPROVE_POLL_INTERVAL: int = 2


def _files_changed_since_baseline(
    project_dir: Path,
    baseline_sha: str,
) -> list[str]:
    """A1 fix: list files changed since baseline SHA.

    Returns relative paths (POSIX). Empty list on error or no baseline.

    Dogfood-8 fix: previously only ``git diff --name-only`` was used —
    which shows **modified tracked files only**. New files created by
    worker (untracked in git) were silently excluded from commit. In
    real dogfood (ses_038a07341ffeKO2qkdB8MWCHA1), auto-commit included
    only ``.gitignore`` while 35 source files stayed untracked.

    Now combines:
    1. ``git diff --name-only <sha>`` — modified tracked files since baseline.
    2. ``git ls-files --others --exclude-standard`` — new untracked files
       (respects .gitignore — awf runtime dirs stay excluded).
    """
    if not baseline_sha:
        return []

    # 1. Modified tracked files
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", baseline_sha],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            logs_dir = project_dir / ".agentic" / "logs"
            if logs_dir.is_dir():
                _log(
                    logs_dir,
                    f"WARNING: git diff vs baseline {baseline_sha!r} failed "
                    f"(rc={result.returncode}): {result.stderr.strip()}",
                )
            return []
        modified = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    except (subprocess.SubprocessError, OSError):
        return []

    # 2. Untracked files (new files worker created since baseline)
    try:
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if untracked_result.returncode == 0:
            untracked = [
                line.strip() for line in untracked_result.stdout.splitlines() if line.strip()
            ]
        else:
            untracked = []
    except (subprocess.SubprocessError, OSError):
        untracked = []

    # Combine + dedupe (a file could be in both lists if it was deleted
    # then re-created). Order: modified first, then new untracked.
    seen: set[str] = set()
    combined: list[str] = []
    for path in [*modified, *untracked]:
        if path and path not in seen:
            seen.add(path)
            combined.append(path)
    return combined


def _commit_specific_files(
    project_dir: Path,
    files: list[str],
    message: str,
) -> bool:
    """A1 fix: commit ONLY the listed files (no `git add -A`).

    Returns True if commit succeeded, False if nothing to commit or error.
    """
    if not files:
        return False
    try:
        # Stage only specific files
        subprocess.run(
            ["git", "add", "--"] + files,
            cwd=project_dir,
            check=True,
            capture_output=True,
        )
        # Commit (allow empty-tree — first commit case)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
    auto: bool = False,
    baseline_sha: str = "",
) -> None:
    """Auto-commit if policy is commit_and_next or commit_and_report.

    In auto mode, blocks waiting for APPROVE-TODO-NNNN.ready signal file
    before committing. This preserves supervisor approval gate.

    A1 fix: if baseline_sha is provided, commits ONLY files changed since
    baseline (avoids mixing supervisor's edits into worker commit).
    Falls back to `git add -A` if no baseline (back-compat).
    """
    if policy not in ("commit_and_next", "commit_and_report"):
        return

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return

    if auto:
        inbox = paths.inbox(project_dir)
        # BD-17: accept either APPROVE-{todo}.ready (from `awf approve` /
        # human) or ACK-{todo}.ready (from supervisor verify subprocess in
        # auto mode). Both authorize the commit.
        approve_signal = inbox / f"APPROVE-{todo_id}.ready"
        ack_signal = inbox / f"ACK-{todo_id}.ready"
        print("Auto-mode: waiting for supervisor approval to commit.", file=sys.stderr)
        print(f"  Approve signal: awf approve {todo_id}", file=sys.stderr)
        print("  Or ACK from supervisor verify subprocess.", file=sys.stderr)
        _log(logs_dir, f"Auto-mode: waiting for APPROVE or ACK signal for {todo_id}")

        # H1 fix: time.monotonic() not time.time() — NTP-immune.
        deadline = time.monotonic() + APPROVE_TIMEOUT_SECONDS
        while not approve_signal.exists() and not ack_signal.exists():
            if time.monotonic() > deadline:
                print(
                    f"ERROR: APPROVE/ACK signal not received within {APPROVE_TIMEOUT_SECONDS}s. "
                    f"Pipeline aborting.",
                    file=sys.stderr,
                )
                _log(logs_dir, f"APPROVE/ACK timeout for {todo_id}")
                raise TimeoutError(f"APPROVE/ACK signal not received for {todo_id}")
            time.sleep(APPROVE_POLL_INTERVAL)
        which = "APPROVE" if approve_signal.exists() else "ACK"
        _log(logs_dir, f"{which} signal received for {todo_id}")

    # A1 fix: commit only baseline-diff files (isolate worker changes)
    if baseline_sha:
        changed = _files_changed_since_baseline(project_dir, baseline_sha)
        if not changed:
            print(f"No changes since baseline — skip commit at '{stage_name}'.", file=sys.stderr)
            _log(logs_dir, f"A1: no diff vs baseline at {stage_name}")
            return
        committed = _commit_specific_files(project_dir, changed, f"awf({stage_name}): {todo_id}")
        _log(logs_dir, f"A1: committed {len(changed)} files (vs baseline {baseline_sha[:8]})")
    else:
        # Back-compat: no baseline → git add -A (legacy behavior)
        committed = git_utils.commit_all(project_dir, f"awf({stage_name}): {todo_id}")
        _log(logs_dir, "committed via git add -A (no baseline provided)")

    if committed:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=project_dir, capture_output=True, text=True,
        ).stdout.strip()
        print(f"Auto-committed: {todo_id} at '{stage_name}' ({sha}).", file=sys.stderr)
        print("Remember to push: git push origin HEAD", file=sys.stderr)
        _log(logs_dir, f"Auto-committed {todo_id} at {stage_name} ({sha})")
    else:
        print(f"No changes to auto-commit at '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"Nothing to auto-commit at {stage_name}")
