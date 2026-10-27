"""Open stage write-violation records: read, classify, refuse.

A violation outlives the attempt that found it: the record in
``.agentic/context/WRITE-VIOLATION-<id>.json`` is the link between the
attempt and the commit gate. This module owns the READ side — one rule for
``record_violation``'s merge and ``open_violations``' check — while the
writer lives in :mod:`awf.stage_manifest`. The split keeps the next edit to
the classification from pushing the manifest module over the line-count
ratchet (TODO-0281 CTX5).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import paths

VIOLATION_PREFIX = "WRITE-VIOLATION-"
VIOLATION_SUFFIX = ".json"

#: K11: a non-dict ``paths`` is corrupt — this name keeps it open.
_CORRUPT_PATHS = "<paths: invalid>"

#: CTX3 (TODO-0281): a recorded ``after`` state is a 64-hex sha256. Any
#: other string cannot be proven to be "the rejected write is gone" —
#: fail closed.
_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def file_hash(path: Path) -> str:
    """One file's manifest state — the snapshot and the record share it.

    ``<missing>`` is the marker both sides compare: a path that vanished
    still has a state in the snapshot, so ``diff`` can name it.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<missing>"


def display_path(path: str) -> str:
    """``path`` safe for strict-UTF-8 report files and logs.

    A git path decoded with ``surrogateescape`` (a non-UTF-8 file name)
    cannot be encoded to UTF-8 as is — the fail-closed report writers would
    raise ``UnicodeEncodeError`` instead of blocking the stage. The escaped
    form keeps the name readable; hashing and the record keep the raw path.
    """
    return os.fsencode(path).decode("utf-8", "backslashreplace")


def violation_path(project_dir: str | Path, todo_id: str) -> Path:
    return paths.context_dir(Path(project_dir)) / f"{VIOLATION_PREFIX}{todo_id}{VIOLATION_SUFFIX}"


def is_recorded_hash(value: object) -> bool:
    """True when ``value`` is a sha256 the record can be proven against."""
    return isinstance(value, str) and _HASH_RE.match(value) is not None


def open_path_names(root: Path, record: dict) -> tuple[tuple[str, ...], bool]:
    """Recorded paths whose content still equals the rejected write.

    ONE rule for both readers: the merge in ``record_violation`` and the
    open check in :func:`open_violations`. Two copies used to decide the
    same thing — they can drift, and then a merge keeps a path the gate
    considers spent (or the reverse: a rejected write no longer withholds
    the commit). A path whose ``after`` state is not a hash cannot be
    proven gone: fail closed. A non-dict ``paths`` is CORRUPT (K11): the
    flag is the single classifier — the name is synthetic, the record stays
    open.
    """
    paths_map = record.get("paths") or {}
    if not isinstance(paths_map, dict):
        return (_CORRUPT_PATHS,), True
    still_open: list[str] = []
    for path, states in paths_map.items():
        after = states.get("after") if isinstance(states, dict) else None
        if not is_recorded_hash(after):
            still_open.append(str(path))
            continue
        if file_hash(root / str(path)) == after:
            still_open.append(str(path))
    return tuple(still_open), False


def still_rejected_paths(root: Path, record: dict) -> dict[str, dict[str, object]]:
    """Path states of an earlier record that still match the rejected write."""
    open_names, corrupt = open_path_names(root, record)
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
        if is_recorded_hash(after):
            kept[name] = {
                "before": states.get("before", "<missing>"),
                "after": after,
            }
        else:
            # CTX3: cannot prove the rejected write is gone — fail closed.
            kept[name] = {"before": "<missing>", "after": None}
    return kept


def _has_unverifiable_path(record: dict, open_paths: tuple[str, ...]) -> bool:
    """True when one OPEN path's recorded ``after`` is not a proven hash.

    The same rule :func:`open_path_names` applies to keep the record open,
    read a second time for the refusal TEXT: such a state cannot be cleared
    by a rollback, so the remedy must offer only an explicit delete.
    """
    paths_map = record.get("paths") or {}
    if not isinstance(paths_map, dict):
        return True  # K11: a corrupt map is exactly such a state
    for name in open_paths:
        states = paths_map.get(name)
        after = states.get("after") if isinstance(states, dict) else None
        if not is_recorded_hash(after):
            return True
    return False


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
    #: True when an open path's recorded ``after`` is not a proven sha256
    #: (``<missing>``, ``null``, a corrupt map): a rollback cannot clear
    #: such a record, only an explicit delete (PA-0281-4).
    unverifiable: bool = False

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
        open_paths, corrupt = open_path_names(root, record)
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
                unverifiable=_has_unverifiable_path(record, open_paths),
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
    elif first.unverifiable:
        # PA-0281-4: a recorded ``after`` that is not a sha256 (``<missing>``,
        # ``null``, a corrupt map) cannot be proven gone — no rollback clears
        # it, only an explicit delete (docs/unit-contract.md, the CTX3
        # paragraph). Advising a rollback sent the owner in a circle.
        remedy = (
            "Review the rejected paths, then delete "
            f"`{context_rel}` to clear the violation — an unverifiable "
            "state cannot be cleared by a rollback."
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


__all__ = [
    "VIOLATION_PREFIX",
    "VIOLATION_SUFFIX",
    "OpenViolation",
    "display_path",
    "file_hash",
    "is_recorded_hash",
    "open_path_names",
    "open_violations",
    "still_rejected_paths",
    "violation_path",
    "violation_refusal",
]
