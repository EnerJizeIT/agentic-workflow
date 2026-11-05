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
overwritten content as probes — the whole file when the reader wrote no
probes of its own, or a merge into the existing probes otherwise (A67:
``findings`` are unioned by id and the reader's stage id is stamped, so the
verdict reader can tell the restored author file from this stage's claims).
The stage runner clears the previous reader's PROBES before the spawn, so
the one present after the stage is this stage's.

A DONE file without a ``stage`` block has no proven owner — old files keep
working exactly as before (nothing is restored).
"""
from __future__ import annotations

import json
from pathlib import Path

from ._atomic import atomic_write_text
from ._log import log as _log
from .signals import find_signal_file
from .unit_contract import (
    findings_of,
    merge_findings,
    parse_done_json,
    stage_owner,
)


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
    """The ``stage.stage_id`` of a DONE json, or ``""`` when it proves nothing.

    The strict reader (a schema-broken file proves no owner) on top of the
    one ownership rule in :func:`unit_contract.stage_owner`.
    """
    return stage_owner(parse_done_json(text))


def _reader_data(text: str) -> dict | None:
    """The reader's DONE as data — strict first, raw when only the schema broke.

    A schema violation (a malformed ``findings`` block is the case this unit
    cares about) still names the owner and may carry a real finding. The
    closed table says "malformed = blocker", so the merge must see the block;
    :func:`unit_contract.findings_of` turns it into that blocker. Unreadable
    JSON proves nothing.
    """
    data = parse_done_json(text)
    if isinstance(data, dict):
        return data
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return raw if isinstance(raw, dict) else None


def _merge_reader_claims(
    probes: Path,
    reader_text: str,
    stage_id: str,
    logs_dir: Path | None = None,
) -> None:
    """A67: keep the reader's findings and identity in its own PROBES file.

    Before this merge, an existing PROBES made ``restore`` drop the reader's
    DONE content entirely — including its structural findings, which are the
    reviewer's actual verdict (a false PASS). Findings are unioned by id (the
    higher severity wins) and the reader's stage id is stamped when PROBES has
    none, so :func:`unit_contract.collect_findings` can tell the restored
    author file from this stage's claims. A malformed block goes through
    :func:`unit_contract.findings_of` as the same blocker the verdict reader
    would produce — one rule on both paths.

    Best-effort: a broken pair is left untouched — this is evidence keeping,
    not a gate.
    """
    reader = _reader_data(reader_text)
    if reader is None:
        return
    try:
        probe_data = json.loads(probes.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return
    if not isinstance(probe_data, dict):
        return

    merged = merge_findings(findings_of(probe_data), findings_of(reader))
    if merged:
        probe_data["findings"] = merged

    owner = stage_owner(reader) or str(stage_id or "").strip()
    stage = probe_data.get("stage")
    if not isinstance(stage, dict):
        stage = {}
    if owner and not str(stage.get("stage_id") or "").strip():
        stage["stage_id"] = owner
    if stage:
        probe_data["stage"] = stage

    try:
        atomic_write_text(probes, json.dumps(probe_data, ensure_ascii=False))
    except OSError as e:
        if logs_dir is not None:
            _log(logs_dir, f"A67: could not merge reader findings into probes: {e}")


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

    # Keep the reader's facts where they belong. A missing PROBES gets the
    # reader's whole DONE content; an existing one (the reader wrote its
    # probes) is MERGED — A67: its findings must survive the restore, and
    # its stage id lets the verdict reader tell the restored author file
    # from this stage's claims.
    probes = probes_path(outbox, todo_id)
    if probes is None and current:
        probes = outbox / f"PROBES-{todo_id}.json"
        try:
            atomic_write_text(probes, current)
        except OSError as e:  # a probe record is evidence, not a gate
            if logs_dir is not None:
                _log(logs_dir, f"A27: could not save auditor probes: {e}")
    elif probes is not None and current:
        _merge_reader_claims(probes, current, stage_id, logs_dir)

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
