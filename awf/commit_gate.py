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

APPROVE_POLL_INTERVAL: int = 2


def _safe_int_env(name: str, default: int) -> int:
    """Parse int from env var with safe fallback on invalid value.

    Without this, ``AWF_APPROVE_TIMEOUT_SECONDS=1800s`` (typo) crashed the
    whole ``awf.commit_gate`` module on import. Now falls back to default
    and logs nothing — typo is a config mistake, not a crash-worthy event.
    """
    raw = os.environ.get(name, str(default))
    try:
        return max(1, int(raw))
    except (ValueError, TypeError):
        return default


def _get_approve_timeout() -> int:
    """P3: lazy evaluation — reads env at call time, not import time.

    Allows tests to set AWF_APPROVE_TIMEOUT_SECONDS via monkeypatch
    without restarting Python.
    """
    return _safe_int_env("AWF_APPROVE_TIMEOUT_SECONDS", 1800)


def _files_changed_since_baseline(
    project_dir: Path,
    baseline_sha: str,
    todo_id: str = "",
) -> list[str]:
    """A1 fix: list files changed since baseline SHA.

    Returns relative paths (POSIX). Empty list on error or no baseline.

    QA-1: ``todo_id`` used to locate ``BASELINE-{todo_id}.untracked`` snapshot
    so pre-existing untracked files are excluded from worker commit.

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
            timeout=30,  # AUD04-11: hung git must not hang the commit gate
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
    # QA-1: exclude pre-existing untracked files (existed before baseline).
    # Read BASELINE-{todo_id}.untracked snapshot to filter them out.
    try:
        untracked_result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,  # AUD04-11
        )
        if untracked_result.returncode == 0:
            untracked = [
                line.strip() for line in untracked_result.stdout.splitlines() if line.strip()
            ]
        else:
            untracked = []
    except (subprocess.SubprocessError, OSError):
        untracked = []

    # QA-1: filter out files that were already untracked at baseline time.
    # BASELINE-{todo_id}.untracked was written by create_baseline().
    baseline_untracked_path: Path | None = None
    if todo_id:
        candidate = project_dir / ".agentic" / "context" / f"BASELINE-{todo_id}.untracked"
        if candidate.exists():
            baseline_untracked_path = candidate
    if not baseline_untracked_path:
        # AUD-2026-08-09.3: no baseline for this todo_id → don't grab another
        # TODO's baseline (would include wrong files). Log warning, include all
        # untracked (safer than filtering with wrong data).
        if todo_id:
            print(
                f"warning: no BASELINE-{todo_id}.untracked found, "
                "including all untracked files in commit",
                file=sys.stderr,
            )

    if baseline_untracked_path and baseline_untracked_path.exists():
        try:
            pre_existing = {
                line.strip()
                for line in baseline_untracked_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            untracked = [f for f in untracked if f not in pre_existing]
        except OSError:
            pass  # best effort — if can't read, include all untracked

    # Combine + dedupe (a file could be in both lists if it was deleted
    # then re-created). Order: modified first, then new untracked.
    seen: set[str] = set()
    combined: list[str] = []
    for path in [*modified, *untracked]:
        if path and path not in seen:
            seen.add(path)
            combined.append(path)
    return combined


def _index_has_foreign_staged(project_dir: Path) -> bool | None:
    """A-01: staged entries already in the index before the gate touches it.

    True — foreign staged changes are present: the unit commit must be
    refused (they would leak into the commit, or a failure rollback would
    unstage them).
    False — index clean, safe to stage the unit files.
    None — could not determine (git error); caller refuses (fail closed).
    """
    try:
        if subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            cwd=project_dir,
            capture_output=True,
            timeout=30,  # AUD04-11
        ).returncode == 0:
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,  # AUD04-11
            )
            if result.returncode not in (0, 1):
                return None
            return result.returncode == 1
        # No commits yet (first-commit case): "foreign staged" is any
        # entry in the index.
        result = subprocess.run(
            ["git", "ls-files", "--stage"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,  # AUD04-11
        )
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return None


def _unstage_unit_files(project_dir: Path, files: list[str]) -> None:
    """A-01: undo exactly the staging this gate did — never a wide reset.

    A plain ``git reset`` on commit failure unstaged the user's foreign
    staged changes (audit 2026-09-25, A-01); restoring only the unit files
    leaves the rest of the index untouched.
    """
    if not files:
        return
    try:
        subprocess.run(
            ["git", "restore", "--staged", "--"] + files,
            cwd=project_dir,
            capture_output=True,
            timeout=30,  # AUD04-11
        )
    except (subprocess.SubprocessError, OSError):
        pass  # best effort; the failure is reported by the caller


def _commit_specific_files(
    project_dir: Path,
    files: list[str],
    message: str,
) -> bool:
    """A1 fix: commit ONLY the listed files (no `git add -A`).

    A-01: refuses when the index already holds staged changes, and on any
    failure unstages only the files this gate added (no wide ``git reset``
    that would drop the user's foreign staged entries).

    Returns True if commit succeeded, False if refused, nothing to commit,
    or error.
    """
    if not files:
        return False
    if _index_has_foreign_staged(project_dir) is not False:
        print(
            "refusing unit commit: the git index already has staged changes. "
            "They would leak into the unit commit or be unstaged on failure; "
            "unstage or commit them first. The index is left untouched.",
            file=sys.stderr,
        )
        return False
    try:
        # Stage only specific files
        subprocess.run(
            ["git", "add", "--"] + files,
            cwd=project_dir,
            check=True,
            capture_output=True,
            timeout=30,  # AUD04-11
        )
        # Commit (allow empty-tree — first commit case)
        result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=30,  # AUD04-11: hung hook/index.lock must not hang verify
        )
        if result.returncode != 0:
            # AUD-2026-08-09.4: commit failed (e.g. pre-commit hook rejection).
            # A-01: unstage exactly the unit files, nothing broader.
            _unstage_unit_files(project_dir, files)
            print(
                f"git commit failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}",
                file=sys.stderr,
            )
            return False
        return True
    except (subprocess.SubprocessError, OSError):
        _unstage_unit_files(project_dir, files)
        return False


def _verdict_refusal(project_dir: Path, todo_id: str) -> str:
    """A-04 (audit 2026-09-25, layer 4): the active run's verdict check.

    A leftover or raced APPROVE/ACK signal must not unlock a commit the
    run already rejected: when the ACTIVE run's diary records
    verdict='rejected' for this TODO, the gate refuses before staging
    anything. Only an active run has verdict authority — outside a run
    there is no diary, and a fresh run resets it (``outcomes={}``), so a
    stale verdict cannot outlive its cycle. A 'generation' marker on the
    entry (cycle identity, when present) that differs from the run's
    generation belongs to another cycle and is not enforced; an
    unreadable marker fails closed (enforce).

    Returns a refusal reason, or "" when the commit may proceed.
    """
    if not todo_id:
        return ""
    try:
        from . import run_state

        state = run_state.read_run(project_dir)
    except Exception:
        return ""  # unreadable run state — the verdict is not provable
    if not state or not state.get("active"):
        return ""
    outcome = (state.get("outcomes") or {}).get(todo_id)
    if not isinstance(outcome, dict) or outcome.get("verdict") != "rejected":
        return ""
    if "generation" in outcome:
        try:
            if int(outcome["generation"]) != run_state.generation_of(state):
                return ""  # verdict of another cycle — not this run's
        except (TypeError, ValueError):
            pass  # unreadable marker — fail closed, enforce the verdict
    reason = str(outcome.get("reason") or "").strip()
    return (
        f"the active run's diary records verdict 'rejected' for {todo_id}"
        + (f" ({reason[:200]})" if reason else "")
    )


def maybe_commit(
    stage_name: str,
    todo_id: str,
    policy: str,
    project_dir: Path,
    logs_dir: Path,
    auto: bool = False,
    baseline_sha: str = "",
) -> bool:
    """Auto-commit if policy is commit_and_next or commit_and_report.

    Returns True if commit succeeded (or was skipped gracefully), False on failure.
    In auto mode, blocks waiting for APPROVE-TODO-NNNN.ready signal file
    before committing. This preserves supervisor approval gate.

    A1 fix: if baseline_sha is provided, commits ONLY files changed since
    baseline (avoids mixing supervisor's edits into worker commit).
    Falls back to `git add -A` if no baseline (back-compat).
    """
    if policy not in ("commit_and_next", "commit_and_report"):
        return True

    if not git_utils.is_git_repo(project_dir):
        print(f"Not a git repo — skipping auto-commit for '{stage_name}'.", file=sys.stderr)
        _log(logs_dir, f"No git repo; auto-commit skipped at {stage_name}")
        return True

    # A-04: the verdict check runs BEFORE the signal wait and before any
    # staging — a rejected unit must not commit even when a leftover
    # APPROVE/ACK file (the reject/approve race) is present.
    refusal = _verdict_refusal(project_dir, todo_id)
    if refusal:
        print(
            f"REFUSING unit commit for {todo_id}: {refusal}. "
            "Nothing was staged; the working tree is left for manual review.",
            file=sys.stderr,
        )
        _log(logs_dir, f"A-04: commit refused for {todo_id} — {refusal}")
        return False

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
        deadline = time.monotonic() + _get_approve_timeout()
        while not approve_signal.exists() and not ack_signal.exists():
            if time.monotonic() > deadline:
                print(
                    f"ERROR: APPROVE/ACK signal not received within {_get_approve_timeout()}s. "
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
        changed = _files_changed_since_baseline(project_dir, baseline_sha, todo_id=todo_id)
        if not changed:
            print(f"No changes since baseline — skip commit at '{stage_name}'.", file=sys.stderr)
            _log(logs_dir, f"A1: no diff vs baseline at {stage_name}")
            return True
        committed = _commit_specific_files(project_dir, changed, f"awf({stage_name}): {todo_id}")
        _log(logs_dir, f"A1: committed {len(changed)} files (vs baseline {baseline_sha[:8]})")
    else:
        # Back-compat: no baseline → git add -A (legacy behavior)
        committed = git_utils.commit_all(project_dir, f"awf({stage_name}): {todo_id}")
        _log(logs_dir, "committed via git add -A (no baseline provided)")

    if committed:
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
                timeout=30,  # AUD04-11
            ).stdout.strip()
        except subprocess.TimeoutExpired:
            print(
                f"git rev-parse timed out at '{stage_name}' — commit result unknown, "
                "treating as failure.",
                file=sys.stderr,
            )
            _log(logs_dir, f"git rev-parse timed out at {stage_name}")
            return False
        print(f"Auto-committed: {todo_id} at '{stage_name}' ({sha}).", file=sys.stderr)
        print("Remember to push: git push origin HEAD", file=sys.stderr)
        _log(logs_dir, f"Auto-committed {todo_id} at {stage_name} ({sha})")
        return True
    else:
        print(f"Commit FAILED at '{stage_name}' — changes remain uncommitted.", file=sys.stderr)
        _log(logs_dir, f"Commit failed at {stage_name} — changes left in working tree")
        return False
