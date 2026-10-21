"""Git helpers — SHA, diff, untracked files, commit, tree fingerprint."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .infra.git_model import GIT_TIMEOUT, GitModelError, GitRepository, display_path


def _git(cwd: Path, *args: str, check: bool = True, errors: str = "strict") -> str:
    """Run a git command and return stdout (strict UTF-8 by default)."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True, errors=errors,
        check=False, timeout=GIT_TIMEOUT,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def git_stdout(
    cwd: Path | str, *args: str, check: bool = True, errors: str = "strict"
) -> str:
    """Public alias for _git — for callers outside this module.

    ``errors="surrogateescape"`` is the lossless read for a human-readable
    ``git log`` subject (arbitrary bytes are legal there); the default stays
    strict so a caller that parses a printed LISTING still sees the decode
    failure instead of a silently aliased path.
    """
    return _git(Path(cwd), *args, check=check, errors=errors)


def current_sha(project_dir: str | Path) -> str:
    """Return the full SHA of HEAD."""
    return _git(Path(project_dir), "rev-parse", "HEAD").strip()


def diff_stat(project_dir: str | Path, baseline_sha: str) -> str:
    """Return git diff --stat output vs baseline."""
    return _git(Path(project_dir), "diff", "--stat", baseline_sha, check=False)


def has_diff(project_dir: str | Path, baseline_sha: str) -> bool:
    """Return True if there are tracked changes vs baseline."""
    try:
        result = subprocess.run(
            ["git", "diff", "--quiet", baseline_sha],
            cwd=Path(project_dir), capture_output=True, check=False, timeout=GIT_TIMEOUT,
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
            cwd=cwd, capture_output=True, check=False, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # AUD01-02: fail closed — report commit failure, no traceback
        return False
    if diff_check.returncode == 0:
        return False  # nothing staged
    try:
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd, capture_output=True, text=True, errors="surrogateescape",
            check=False, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False
    if commit_result.returncode != 0:
        # Pre-commit hook rejection, signing failed, etc.
        return False
    return True


def status_porcelain(project_dir: str | Path) -> str:
    """Return ``git status --porcelain`` (empty = clean tree).

    M4: the listing is printed, so a non-UTF-8 name under
    ``core.quotePath=false`` cannot be decoded. It becomes a non-empty
    marker, never an empty string: callers read "empty" as "clean" (the
    mutation dirty-gate refuses on a non-empty status), and an unknown
    tree must not pass for a clean one.
    """
    try:
        return _git(Path(project_dir), "status", "--porcelain", check=False)
    except UnicodeDecodeError:
        return "?? <non-UTF-8 path — status undecodable>\n"


def tree_fingerprint(project_dir: str | Path) -> str:
    """U11: content-addressed hash of the EXACT working-tree state.

    Covers what ``awf`` commits: HEAD, every tracked change (staged +
    unstaged, binary-safe via ``--binary``), and untracked non-ignored
    files with their content hashes. Same state → same hash, at any time
    (no timestamps involved); a new commit, an edited tracked file, or a
    new/changed/deleted untracked file → different hash.

    The untracked NAMES come from the model (``-z`` bytes, ``display_path``
    identity), not a printed ``ls-files``: that read C-quoted a non-ASCII
    name (so the hash keyed it by an alias other readers never use) and, with
    ``core.quotePath=false``, a non-UTF-8 name raised ``UnicodeDecodeError``
    and aborted the whole fingerprint. An ASCII tree whose names the printed
    read left untouched keeps its old digest (same names, order and
    encoding). A name the printed ``ls-files`` C-quoted — a non-ASCII name,
    or an ASCII one with a tab, a quote, a backslash or a control character
    — now hashes in the model's RAW identity (the identity every other
    reader speaks): the digest changes for such a tree, and only for it.
    The ``status`` read is a hash INPUT, never parsed for paths, and decodes
    with ``surrogateescape`` so an unusual name cannot crash it.

    No side effects: the index and the working tree are never modified
    (``git stash create`` is NOT used — it drops untracked-only states).
    Raises RuntimeError when the directory is not a git repo or has no
    commits yet (callers wrap into a user-facing error).
    """
    pd = Path(project_dir)
    if not is_git_repo(pd):
        raise RuntimeError(f"{pd} is not a git repo")
    head = _git(pd, "rev-parse", "HEAD").strip()  # RuntimeError: no commits
    h = hashlib.sha256()
    h.update(b"head\n" + head.encode("utf-8"))
    status = _git(pd, "status", "--porcelain", check=False, errors="surrogateescape")
    h.update(b"status\n" + status.encode("utf-8", errors="surrogateescape"))
    diff = _git(pd, "diff", "--binary", "HEAD", check=False)
    h.update(b"diff\n" + diff.encode("utf-8", errors="replace"))
    try:
        names = GitRepository(pd).untracked_paths()
    except GitModelError:
        # The rest of the read degrades, not aborts: a git that refuses the
        # repository (an unreadable ``.git/info/exclude``, the M1 scenario)
        # previously produced a fingerprint without untracked names.
        names = []
    untracked = [display_path(p) for p in names]
    h.update(b"untracked\n" + "\n".join(sorted(untracked)).encode("utf-8", "surrogateescape"))
    for name in untracked:
        path = pd / name
        if path.is_file():
            content_hash = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    content_hash.update(chunk)
            h.update(name.encode("utf-8", "surrogateescape") + b"\0" + content_hash.digest())
    return h.hexdigest()


def is_git_repo(project_dir: str | Path) -> bool:
    """Return True if project_dir is inside a git repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=Path(project_dir), capture_output=True, check=False, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # AUD01-02: fail closed — treat as not a repo, no traceback
        return False
    return result.returncode == 0
