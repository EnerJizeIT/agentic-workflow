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

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .infra import managed_fs

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
        path = root / name
        try:
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            result[name] = "<missing>"
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


def display_path(path: str) -> str:
    """``path`` safe for strict-UTF-8 report files and logs.

    A git path decoded with ``surrogateescape`` (a non-UTF-8 file name)
    cannot be encoded to UTF-8 as is — the fail-closed report writers would
    raise ``UnicodeEncodeError`` instead of blocking the stage. The escaped
    form keeps the name readable; hashing and the record keep the raw path.
    """
    return os.fsencode(path).decode("utf-8", "backslashreplace")


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

VIOLATION_PREFIX = "WRITE-VIOLATION-"
VIOLATION_SUFFIX = ".json"


def _file_hash(path: Path) -> str:
    """One file's manifest state — the same ``<missing>`` marker as snapshot."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<missing>"


def violation_path(project_dir: str | Path, todo_id: str) -> Path:
    return paths.context_dir(Path(project_dir)) / f"{VIOLATION_PREFIX}{todo_id}{VIOLATION_SUFFIX}"


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
        paths = _still_rejected_paths(root, previous)
    for path in paths_outside:
        paths[path] = {
            "before": before.get(path, "<missing>"),
            "after": _file_hash(root / path),
        }
    record = {
        "todo_id": todo_id,
        "stage": stage_name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "allowlist": list(allowlist),
        "paths": paths,
        "error": error or previous_error,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    managed_fs.managed_write_text(
        target,
        # ensure_ascii: a non-UTF-8 path arrives with surrogate escapes and
        # would raise UnicodeEncodeError from the fail-closed branch; the
        # JSON escape keeps it reversible for the hash check on read.
        json.dumps(record, ensure_ascii=True, indent=2) + "\n",
        root=managed_fs.agentic_root_for(target),
    )
    return target


#: K11: a non-dict ``paths`` is corrupt — this name keeps it open.
_CORRUPT_PATHS = "<paths: invalid>"


def _open_path_names(root: Path, record: dict) -> tuple[tuple[str, ...], bool]:
    """Recorded paths whose content still equals the rejected write.

    ONE rule for both readers: the merge in :func:`record_violation` and the
    open check in :func:`open_violations`. Two copies used to decide the same
    thing — they can drift, and then a merge keeps a path the gate considers
    spent (or the reverse: a rejected write no longer withholds the commit).
    A path whose ``after`` state is not a hash cannot be proven gone: fail
    closed. A non-dict ``paths`` is CORRUPT (K11): the flag is the single
    classifier — the name is synthetic, the record stays open.
    """
    paths = record.get("paths") or {}
    if not isinstance(paths, dict):
        return (_CORRUPT_PATHS,), True
    still_open: list[str] = []
    for path, states in paths.items():
        after = states.get("after") if isinstance(states, dict) else None
        if not isinstance(after, str):
            still_open.append(str(path))
            continue
        if _file_hash(root / str(path)) == after:
            still_open.append(str(path))
    return tuple(still_open), False


def _still_rejected_paths(root: Path, record: dict) -> dict[str, dict[str, object]]:
    """Path states of an earlier record that still match the rejected write."""
    open_names, corrupt = _open_path_names(root, record)
    if corrupt:
        # K11: a corrupt record cannot be proven gone — keep it open.
        return {_CORRUPT_PATHS: {"before": "<missing>", "after": None}}
    raw = record.get("paths") or {}
    open_set = set(open_names)
    kept: dict[str, dict[str, object]] = {}
    for path, states in raw.items():
        name = str(path)
        if name not in open_set:
            continue
        after = states.get("after") if isinstance(states, dict) else None
        if isinstance(after, str):
            kept[name] = {
                "before": states.get("before", "<missing>"),
                "after": after,
            }
        else:
            # Cannot prove the rejected write is gone — fail closed.
            kept[name] = {"before": "<missing>", "after": None}
    return kept


@dataclass(frozen=True)
class OpenViolation:
    """A recorded violation that still withholds the commit."""

    todo_id: str
    stage: str
    paths: tuple[str, ...]
    error: str = ""
    #: The record file stem — the actual name of the file to delete, which
    #: may differ from ``todo_id`` (the value written inside the record).
    record_name: str = ""

    def reason(self) -> str:
        parts: list[str] = []
        if self.paths:
            parts.append(
                f"stage '{self.stage}' wrote outside its allowlist: "
                + ", ".join(display_path(path) for path in self.paths)
            )
        if self.error:
            parts.append(f"the write manifest was unavailable ({self.error})")
        return "; ".join(parts) or f"stage '{self.stage}': open write violation"


def open_violations(project_dir: str | Path) -> list[OpenViolation]:
    """Every recorded violation still withholding a commit, by record name.

    The order is the sorted record file name (unit id), not the newest
    ``recorded_at``; the refusal names the first record in that order.

    An unreadable record fails closed (it withholds the commit): the gate
    must never pass because a trace file was corrupted.
    """
    root = Path(project_dir)
    context = paths.context_dir(root)
    if not context.is_dir():
        return []
    found: list[OpenViolation] = []
    for record_file in sorted(context.glob(f"{VIOLATION_PREFIX}*{VIOLATION_SUFFIX}")):
        stem = record_file.name[len(VIOLATION_PREFIX):-len(VIOLATION_SUFFIX)]
        try:
            record = json.loads(record_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            found.append(
                OpenViolation(stem, "?", (), f"record unreadable: {e}", record_name=stem)
            )
            continue
        if not isinstance(record, dict):
            found.append(
                OpenViolation(stem, "?", (), "record is not an object", record_name=stem)
            )
            continue
        open_paths, corrupt = _open_path_names(root, record)
        error = str(record.get("error") or "")
        if error:
            # A manifest was unavailable at some point: only an explicit
            # removal clears it (the stage may have run unfenced). Keeping it
            # open alongside still-rejected paths is the merged record.
            found.append(
                OpenViolation(
                    todo_id=str(record.get("todo_id") or stem),
                    stage=str(record.get("stage") or "?"),
                    paths=open_paths,
                    error=error,
                    record_name=stem,
                )
            )
            continue
        if not corrupt:
            if record.get("paths") and not open_paths:
                continue  # every rejected path is gone — the record is spent
            if not record.get("paths"):
                found.append(
                    OpenViolation(stem, "?", (), "record names no path", record_name=stem)
                )
                continue
        found.append(
            OpenViolation(
                todo_id=str(record.get("todo_id") or stem),
                stage=str(record.get("stage") or "?"),
                paths=open_paths,
                record_name=stem,
            )
        )
    return found


def violation_refusal(project_dir: str | Path) -> str:
    """The commit refusal while any write violation is open, else ``""``."""
    open_ones = open_violations(project_dir)
    if not open_ones:
        return ""
    first = open_ones[0]
    # P2 (TODO-0257): resolve the root BEFORE relativizing (a relative
    # ``project_dir`` made ``relative_to`` raise), and name the record by
    # its actual file stem — the ``todo_id`` inside may not match it, and
    # the remedy must point at the file that really blocks the commit.
    root = Path(project_dir).resolve()
    record = violation_path(root, first.record_name or first.todo_id)
    # P3 (TODO-0257 REVIEW): the report/refusal text is strict UTF-8; a
    # unit id carried by a non-UTF-8 inbox name arrives with surrogates.
    # Escape for the text, keep the raw name for the path on disk.
    context_rel = display_path(record.relative_to(root).as_posix())
    if first.error:
        # An unavailable manifest cannot be cleared by a rollback alone; the
        # record stays open until it is removed explicitly (AUD4).
        remedy = (
            "Fix the rejected paths (if any), re-run the stage once the "
            f"manifest can be taken, then delete `{context_rel}` to clear "
            "the violation."
        )
    elif first.paths:
        remedy = (
            "Roll the path back (or rewrite it), then delete "
            f"`{context_rel}` to clear the violation."
        )
    else:
        remedy = (
            "Re-run the stage once the manifest can be taken, then delete "
            f"`{context_rel}` to clear the violation."
        )
    return (
        f"K5: open write violation — {first.reason()}. The rejected write "
        f"is not committed. {remedy}"
    )
