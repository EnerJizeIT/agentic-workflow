"""RUN10 #4 (TODO-0074): pre-existing untracked — visibility + conscious
inclusion into a unit's commit.

The commit gate (awf/commit_gate.py::_files_changed_since_baseline) commits
only changes since the unit baseline: a file that was untracked BEFORE the
baseline (recorded in ``BASELINE-<todo>.untracked``) is excluded from the
unit commit. That protection is right, but it used to be invisible — the
dispatch answer said nothing and the only way to include a pre-existing file
was a worker's ``git add`` (source-code knowledge).

Two parts:

1. Visibility — :func:`pre_existing_untracked` reads the baseline snapshot
   (the exact list the gate will exclude) and
   :func:`format_untracked_warning` renders the capped one-line warning the
   dispatch answer carries.

2. Inclusion — :func:`resolve_include_untracked` validates the requested
   paths (exist, untracked, not gitignored, inside the project; all-or-
   nothing, no side effects). The dispatch then excludes them from the
   baseline's untracked snapshot — the same mechanism the leak-gate
   carry-over uses, kept as a separate parameter so the two mechanisms stay
   independent. The trace lives in ``BASELINE-<todo>.include``
   (:func:`write_include_audit` / :func:`read_include_list` /
   :func:`clear_include_audit`); the run re-baseline re-applies it, together
   with the carry-over and claim links, through
   :func:`awf.api.hygiene.transfer_exclusions`.
"""
from __future__ import annotations

from pathlib import Path

from . import paths
from ._errors import AwfApiError
from ._log import log as _log
from ._path_link import decode_path_link, encode_path_link
from .infra import managed_fs

#: The dispatch warning lists at most this many files (the rest as "…N more").
WARNING_CAP = 10


def include_file_path(project_dir: Path, todo_id: str) -> Path:
    """``.agentic/context/BASELINE-<todo>.include`` for a dispatch with
    ``include_untracked``."""
    return paths.context_dir(Path(project_dir)) / f"BASELINE-{todo_id}.include"


def read_include_list(project_dir: Path, todo_id: str) -> list[str] | None:
    """Paths recorded in ``BASELINE-<todo>.include`` (sorted); None when the
    file is missing (a dispatch without include_untracked) or not a readable
    path list.

    The link is read through the shared byte-faithful codec
    (:mod:`awf._path_link`): a name with a newline / control byte / edge space
    round-trips exactly, or the adopted exclusion is silently lost on a
    re-baseline. Binary content degrades to None (NEG-3)."""
    f = include_file_path(project_dir, todo_id)
    if not f.is_file():
        return None
    try:
        return decode_path_link(f.read_bytes())
    except OSError:
        return None


def resolve_include_untracked(project_dir: Path, raw_paths: list[str]) -> list[str]:
    """Validate ``include_untracked`` paths; return them as sorted relative
    POSIX paths.

    Every path must exist (a file, not a directory), be untracked, be not
    gitignored, and stay inside the project. All-or-nothing: the FIRST
    invalid path raises before any path is accepted — the caller runs this
    BEFORE reserving the TODO id, so a refusal leaves no side effects (no
    TODO, no baseline), the same contract as the leak-gate carry-over.

    Raises:
        AwfApiError: empty list, empty entry, absolute path, ``..`` in the
            path, no such file, a directory, already tracked, gitignored, or
            git cannot list untracked files.
    """
    project_dir = Path(project_dir)
    raw = list(raw_paths or [])
    if not raw:
        raise AwfApiError(
            "include_untracked: empty list — pass the untracked paths to "
            "include, or omit the parameter"
        )
    rel: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise AwfApiError(f"include_untracked: empty/invalid path entry {entry!r}")
        if Path(entry).is_absolute():
            raise AwfApiError(
                f"include_untracked '{entry}': absolute paths are not allowed — "
                "pass a project-relative path"
            )
        if ".." in Path(entry).parts:
            raise AwfApiError(
                f"include_untracked '{entry}': '..' is not allowed — pass a "
                "project-relative path without '..'"
            )
        fs = project_dir / entry
        if not fs.exists():
            raise AwfApiError(f"include_untracked '{entry}': no such file in the project")
        if fs.is_dir():
            raise AwfApiError(
                f"include_untracked '{entry}': is a directory — include single files"
            )
        rel.append(Path(entry).as_posix())

    # Read the tree through the R2.1 model (one git parser, byte-faithful
    # paths): the printed ``git ls-files`` C-quotes a non-ASCII name, so a
    # request for ``файл.txt`` would never match the quoted listing and the
    # file would be refused as "gitignored" (TODO-0221 M1).
    from .infra.git_model import GitModelError, GitRepository, display_path

    try:
        repo = GitRepository(project_dir)
        untracked_set = {display_path(p) for p in repo.untracked_paths()}
        tracked = {display_path(e.path) for e in repo.index_entries()}
    except GitModelError as e:
        raise AwfApiError(
            "include_untracked: cannot list untracked files (not a git repo or "
            f"git failed): {e} — cannot validate the paths"
        ) from e
    seen: set[str] = set()
    for rp in rel:
        if rp in seen:
            continue
        seen.add(rp)
        if rp in untracked_set:
            continue
        if rp in tracked:
            raise AwfApiError(
                f"include_untracked '{rp}': already tracked by git — its changes "
                "join the unit commit automatically, no include_untracked needed"
            )
        raise AwfApiError(
            f"include_untracked '{rp}': the file is gitignored (or otherwise "
            "invisible to git) — remove the ignore rule or keep it excluded"
        )
    return sorted(seen)


def pre_existing_untracked(project_dir: Path, todo_id: str) -> list[str]:
    """The baseline's pre-existing untracked list (what the commit gate will
    exclude), RAW; sorted; ``[]`` when the snapshot is absent or empty."""
    from .infra.git_model import read_untracked_snapshot

    return sorted(read_untracked_snapshot(project_dir, todo_id) or set())


def format_untracked_warning(
    todo_id: str, files: list[str], cap: int = WARNING_CAP
) -> str:
    """One-line dispatch warning for files excluded from the unit commit.
    ``""`` when the list is empty (nothing to say)."""
    if not files:
        return ""
    from .infra.git_model import escape_str_for_ui

    shown = [escape_str_for_ui(p) for p in files[:cap]]
    more = len(files) - len(shown)
    tail = f" (…{more} more)" if more else ""
    return (
        f"⚠️ {len(files)} file(s) were already untracked before this unit and "
        f"will NOT be included in the {todo_id} commit: "
        f"{', '.join(shown)}{tail}. "
        "To include any of them consciously, re-dispatch with "
        "include_untracked=[...]."
    )


def write_include_audit(project_dir: Path, todo_id: str, files: list[str]) -> None:
    """Best-effort audit trace (same contract as the carry-over link): a
    failed write logs but does not undo the dispatch — the exclusion is
    already in the baseline snapshot."""
    try:
        # The paths are RAW (``display_path``: ``surrogateescape``); the shared
        # codec writes each entry NUL-terminated so a newline / control byte /
        # edge space in a name round-trips byte-faithfully. Strict UTF-8 would
        # raise ``UnicodeEncodeError`` AFTER the TODO reservation and leave a
        # half-dispatch (no ``.ready``/``.include``), so the write keeps
        # ``surrogateescape`` too.
        # AUD-0282-1: managed ``context`` writer (symlink refuses, best-effort).
        target = include_file_path(project_dir, todo_id)
        managed_fs.managed_write_under_agentic(
            target, encode_path_link(files), errors="surrogateescape"
        )
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — include_untracked: {len(files)} "
            "pre-existing untracked file(s) re-claimed into the unit commit",
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        _log(
            paths.logs_dir(project_dir),
            f"dispatch: {todo_id} — include audit link not written: {e}",
        )


def clear_include_audit(project_dir: Path, todo_id: str) -> None:
    """Drop a stale include link: a (re-)dispatch without include_untracked
    rewrites the baseline WITHOUT the exclusion, so a later re-baseline
    (awf run_next) must not re-apply an inclusion that no longer holds —
    the same lifecycle as the carry-over link."""
    include_file_path(project_dir, todo_id).unlink(missing_ok=True)


__all__ = [
    "WARNING_CAP",
    "clear_include_audit",
    "format_untracked_warning",
    "include_file_path",
    "pre_existing_untracked",
    "read_include_list",
    "resolve_include_untracked",
    "write_include_audit",
]
