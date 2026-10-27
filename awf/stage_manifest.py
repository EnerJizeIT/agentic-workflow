"""SPEC IMP5 (K5): the stage write manifest — what a stage touched.

The engine snapshots the tree before and after a stage with a declared
allowlist (``output`` + ``write_allow``). The difference is the set of
paths the stage touched; a path outside the allowlist turns the stage into
``BLOCKED`` (the file stays, nothing is committed, nothing is rolled back —
doctrine 02).

The manifest covers tracked files and new non-ignored files. The engine's
own runtime surfaces (``.agentic/inbox``, ``outbox``, ``state``, …) are not
the worker's write and are excluded: they change on every stage by design.
Gitignored files are excluded too — the same rule the commit gate uses.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .infra import managed_fs
from .violations import (
    VIOLATION_PREFIX,
    VIOLATION_SUFFIX,
    OpenViolation,
    display_path,
    file_hash,
    open_violations,
    still_rejected_paths,
    violation_path,
    violation_refusal,
)

#: Engine-written runtime directories, relative to the project root. ONE
#: source: the same list the .gitignore block and the dirty policy read.
_RUNTIME_PREFIXES: tuple[str, ...] = tuple(
    f".agentic/{name}" for name in paths.RUNTIME_DIRS
)


def _git(project_dir: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {' '.join(args)} timed out") from None
    except OSError as e:
        # AUD4 (QA TODO-0256): a missing binary is "no git" too. Only a
        # timeout and a non-zero rc used to be normalized; FileNotFoundError
        # escaped the snapshot and crashed the pipeline instead of BLOCKED.
        raise RuntimeError(f"git {' '.join(args)} unavailable: {e}") from None
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: "
            f"{proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode("utf-8", "surrogateescape")


def _relative_files(project_dir: Path) -> list[str]:
    """Tracked + new non-ignored files, project-relative, runtime excluded."""
    raw = _git(
        project_dir,
        "ls-files",
        "-z",
        "--cached",
        "--others",
        "--exclude-standard",
    )
    files: list[str] = []
    for name in raw.split("\0"):
        if not name:
            continue
        if any(name == prefix or name.startswith(prefix + "/") for prefix in _RUNTIME_PREFIXES):
            continue
        files.append(name)
    return files


def snapshot(project_dir: str | Path) -> dict[str, str]:
    """``path -> sha256`` at this instant (``<missing>`` for vanished files)."""
    root = Path(project_dir)
    result: dict[str, str] = {}
    for name in sorted(set(_relative_files(root))):
        # One hash rule for the snapshot and the violation record
        # (``violations.file_hash``) — a second inline copy here could
        # drift on the ``<missing>`` marker the gate relies on.
        result[name] = file_hash(root / name)
    return result


def diff(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Paths added, removed or changed between two snapshots."""
    return sorted(
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )


def _normalise(path: str) -> str:
    """Compare form of a declared/emitted path: no leading ``./``, no ``//``.

    A pipeline may declare ``output: ./docs/out.md`` while git reports
    ``docs/out.md`` — the same file. Normalising both sides of
    :func:`is_allowed` keeps that spelling from becoming a false BLOCKED.
    """
    parts = [part for part in path.split("/") if part and part != "."]
    return "/".join(parts)


def is_allowed(path: str, allowlist: tuple[str, ...]) -> bool:
    """A touched path is the allowlist entry itself or lives under it."""
    normalised = _normalise(path)
    for allowed in allowlist:
        entry = _normalise(allowed)
        if normalised == entry or normalised.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def violations(touched: list[str], allowlist: tuple[str, ...]) -> list[str]:
    return [path for path in touched if not is_allowed(path, allowlist)]


# ── open violations (AUD2, TODO-0256) ───────────────────────────────────
#
# A violation outlives the attempt that found it: an ACK re-runs the stage
# with the rejected path already in the "before" snapshot, and the commit
# uses ``git add -A``. The record below is the link between the attempt and
# the commit gate: while it is open, the plan refuses.

def record_violation(
    project_dir: str | Path,
    todo_id: str,
    stage_name: str,
    *,
    allowlist: tuple[str, ...] | list[str] = (),
    paths_outside: list[str] | tuple[str, ...] = (),
    before: dict[str, str] | None = None,
    error: str = "",
) -> Path:
    """Persist the OPEN write violation of ``todo_id`` (commit gate input).

    ``error`` is the manifest-unavailable reason (AUD4): such a record stays
    open until the file is removed explicitly.

    A later violation on the SAME unit merges into the record instead of
    replacing it: an earlier rejected path whose bytes are still in the tree
    keeps withholding the commit. Replacing the file used to free the first
    path on an ACK/repeat that happened to write another bad path — exactly
    the guarantee AUD2 asks for ("ACK не превращает забракованный путь в
    коммит"). A path record closes itself once the file content stops
    matching the recorded post-stage state — rolled back, deleted or
    intentionally rewritten; there is nothing left of the rejected write to
    withhold.
    """
    root = Path(project_dir)
    before = before or {}
    target = violation_path(root, todo_id)
    previous_error = ""
    paths: dict[str, dict[str, object]] = {}
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    if isinstance(previous, dict):
        previous_error = str(previous.get("error") or "")
        paths = still_rejected_paths(root, previous)
    for path in paths_outside:
        paths[path] = {
            "before": before.get(path, "<missing>"),
            "after": file_hash(root / path),
        }
    record = {
        "todo_id": todo_id,
        "stage": stage_name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "allowlist": list(allowlist),
        "paths": paths,
        "error": error or previous_error,
    }
    # AUD3 (REVIEW 0281): the record's directory is a managed surface like
    # every other context writer — a bare ``mkdir`` follows a symlinked
    # ``.agentic``/``context`` and creates the record outside the project.
    managed_fs.managed_mkdir(target.parent)
    managed_fs.managed_write_text(
        target,
        # ensure_ascii: a non-UTF-8 path arrives with surrogate escapes and
        # would raise UnicodeEncodeError from the fail-closed branch; the
        # JSON escape keeps it reversible for the hash check on read.
        json.dumps(record, ensure_ascii=True, indent=2) + "\n",
        root=managed_fs.agentic_root_for(target),
    )
    return target


# ── read side (CTX5, TODO-0281) ─────────────────────────────────────────
#
# The open-violation reader and classifier live in :mod:`awf.violations`:
# the read side is what the next edit grows, and keeping it here pushed the
# module over the line-count ratchet. ``record_violation`` above is the
# writer; the names this module published BEFORE the split stay importable
# from here (``__all__`` below), so no caller has to learn a new import path
# for the move alone.

__all__ = [
    "VIOLATION_PREFIX",
    "VIOLATION_SUFFIX",
    "OpenViolation",
    "diff",
    "display_path",
    "is_allowed",
    "open_violations",
    "record_violation",
    "snapshot",
    "violation_path",
    "violation_refusal",
    "violations",
]
