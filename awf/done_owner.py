"""A27: ``DONE-<id>.json`` belongs to the stage whose report it describes.

Every stage overwrites ``outbox/DONE-<id>.json`` in practice, so a reviewer
or auditor erased the implementer's facts — ``tests_run`` lost the author's
commands and gained the auditor's prose. The engine cannot stop a worker
from writing a file, but it can notice that the file it found before the
stage was authored by ANOTHER stage and put it back afterwards.

Rule: the file's own ``stage.stage_id`` names its owner. The owner may
rewrite its file (retries, fix rounds). Any other stage is a reader: its
facts go to ``PROBES-<id>.json``. When such a stage overwrites DONE anyway
(or deletes it), the engine restores the author's bytes and keeps the
overwritten content as probes when the reader wrote no probes of its own.
The stage runner clears the previous reader's PROBES before the spawn, so
the one present after the stage is this stage's.

A DONE file without a ``stage`` block has no proven owner — old files keep
working exactly as before (nothing is restored).
"""
from __future__ import annotations

from pathlib import Path

from ._atomic import atomic_write_text
from ._log import log as _log
from .signals import find_signal_file
from .unit_contract import parse_done_json


def done_path(outbox: Path, todo_id: str) -> Path | None:
    """The unit's DONE json — canonical spelling first, legacy short second.

    One lookup for every reader (handoff, verify pack, this module): the
    file the guard restores must be the file A8 checks.
    """
    return find_signal_file(outbox, "DONE", todo_id, ".json")


def probes_path(outbox: Path, todo_id: str) -> Path | None:
    """The reader's own claims file, when it already exists."""
    return find_signal_file(outbox, "PROBES", todo_id, ".json")


def snapshot(outbox: Path, todo_id: str) -> str | None:
    """The DONE json text as it is BEFORE the stage runs (``None`` if absent)."""
    path = done_path(outbox, todo_id)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _declared_owner(text: str) -> str:
    """The ``stage.stage_id`` of a DONE json, or ``""`` when it proves nothing."""
    data = parse_done_json(text)
    if not isinstance(data, dict):
        return ""
    stage = data.get("stage")
    if not isinstance(stage, dict):
        return ""
    return str(stage.get("stage_id") or "").strip()


def restore(
    outbox: Path,
    todo_id: str,
    *,
    stage_id: str,
    snapshot_text: str | None,
    logs_dir: Path | None = None,
) -> bool:
    """Undo a foreign stage's overwrite of ``DONE-<id>.json``.

    Returns True when the author's file was put back. A no-op (False) when
    there was no file before the stage, when the file has no owner, when
    this stage IS the owner, or when the stage did not touch the file.
    A stage that DELETED the file is an overwrite too: the author's bytes
    come back.
    """
    if not snapshot_text:
        return False
    owner = _declared_owner(snapshot_text)
    if not owner or owner == stage_id:
        return False

    path = done_path(outbox, todo_id)
    current = ""
    if path is not None:
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = ""
        if current == snapshot_text:
            return False
    else:
        # The stage removed the author's file; restore writes it back.
        path = outbox / f"DONE-{todo_id}.json"

    # Keep the reader's facts where they belong. An existing PROBES file is
    # the reader's own and wins — the duplicated DONE copy is dropped.
    probes = probes_path(outbox, todo_id)
    if probes is None and current:
        probes = outbox / f"PROBES-{todo_id}.json"
        try:
            atomic_write_text(probes, current)
        except OSError as e:  # a probe record is evidence, not a gate
            if logs_dir is not None:
                _log(logs_dir, f"A27: could not save auditor probes: {e}")

    try:
        atomic_write_text(path, snapshot_text)
    except OSError as e:
        if logs_dir is not None:
            _log(logs_dir, f"A27: could not restore {path.name} to its owner {owner}: {e}")
        return False
    if logs_dir is not None:
        _log(
            logs_dir,
            f"A27: {path.name} belongs to {owner}; {stage_id or 'this stage'} "
            "wrote over it — author's facts restored",
        )
    return True


__all__ = ["done_path", "probes_path", "restore", "snapshot"]
