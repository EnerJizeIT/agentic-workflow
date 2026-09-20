"""Git helpers — SHA, diff, untracked files, commit."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, check=False, timeout=30,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_stdout(cwd: Path | str, *args: str, check: bool = True) -> str:
    """Public alias for _git — for callers outside this module."""
    return _git(Path(cwd), *args, check=check)


def current_sha(project_dir: str | Path) -> str:
    """Return the full SHA of HEAD."""
    return _git(Path(project_dir), "rev-parse", "HEAD").strip()


def diff_stat(project_dir: str | Path, baseline_sha: str) -> str:
    """Return git diff --stat output vs baseline."""
    return _git(Path(project_dir), "diff", "--stat", baseline_sha, check=False)


def untracked_files(project_dir: str | Path) -> list[str]:
    """Return list of untracked (non-ignored) files."""
    out = _git(Path(project_dir), "ls-files", "--others", "--exclude-standard", check=False)
    return [f for f in out.splitlines() if f]


def has_diff(project_dir: str | Path, baseline_sha: str) -> bool:
    """Return True if there are tracked changes vs baseline."""
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", baseline_sha],
            cwd=Path(project_dir), capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        # AUD01-02: fail closed — no evidence of work, no traceback
        return False
    return result.returncode != 0


def commit_all(project_dir: str | Path, message: str) -> bool:
    """Stage all changes and commit. Returns False if nothing to commit
    or commit failed (e.g. pre-commit hook rejection).

    H1 fix: was returning True unconditionally after `git commit` call.
    Now checks returncode — if pre-commit hook rejects, returns False so
    callers (commit_gate.maybe_commit) report failure correctly.
    """
    cwd = Path(project_dir)
    try:
        _git(cwd, "add", "-A", check=False)
        diff_check = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=cwd, capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        # AUD01-02: fail closed — report commit failure, no traceback
        return False
    if diff_check.returncode == 0:
        return False  # nothing staged
    try:
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd, capture_output=True, text=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    if commit_result.returncode != 0:
        # Pre-commit hook rejection, signing failed, etc.
        return False
    return True


def is_git_repo(project_dir: str | Path) -> bool:
    """Return True if project_dir is inside a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=Path(project_dir), capture_output=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        # AUD01-02: fail closed — treat as not a repo, no traceback
        return False
    return result.returncode == 0
