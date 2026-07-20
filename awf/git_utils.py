"""Git helpers — SHA, diff, untracked files, commit."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


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
    result = subprocess.run(
        ["git", "diff", "--quiet", baseline_sha],
        cwd=Path(project_dir), capture_output=True, check=False,
    )
    return result.returncode != 0


def commit_all(project_dir: str | Path, message: str) -> bool:
    """Stage all changes and commit. Returns False if nothing to commit."""
    cwd = Path(project_dir)
    _git(cwd, "add", "-A", check=False)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=cwd, capture_output=True, check=False,
    )
    if result.returncode == 0:
        return False  # nothing staged
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=cwd, capture_output=True, check=False,
    )
    return True


def is_git_repo(project_dir: str | Path) -> bool:
    """Return True if project_dir is inside a git repo."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=Path(project_dir), capture_output=True, check=False,
    )
    return result.returncode == 0
