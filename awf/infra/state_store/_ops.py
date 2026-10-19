"""R1.3: the durable operation journal.

The PATH and KEY are shared with R1.2 ``hygiene`` (``api/hygiene.py``):
one ``state/operations/<sha256(id)>.json`` per operation id — a
user-supplied token is never a path component. Since TODO-0227 (M3) the
RECORD SCHEMA is shared too: :mod:`._journal` owns the status vocabulary
(reserved/running/applied/refused/failed/recovery_required), the ``ts``
format and the arguments digest, and BOTH writers build their line through
it. A legacy hygiene line (``started``) is still readable — dual read maps
it to ``running`` — but neither writer emits that vocabulary any more.

A retry reads the recorded outcome instead of guessing; an unreadable
record is unknown, never "never started".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..._errors import OperationRecoveryRequired
from .. import managed_fs
from ._errors import StateStoreError
from ._io import atomic_write_json, operations_dir, sha256
from ._journal import build_record, parse

_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_operation_id(operation_id: str) -> None:
    if not _OPERATION_ID_RE.match(operation_id or ""):
        raise StateStoreError(
            f"invalid operation_id {operation_id!r} — letters, digits, '.', "
            "'_', '-' only (1..64 chars, starts alphanumeric)"
        )


def op_path(project_dir: str | Path, operation_id: str) -> Path:
    """R1.2 (TODO-0216, F4): the journal path for ``operation_id``.

    The leaf is a digest of the id, never the id itself. The id is a
    user-supplied token: verbatim it could be a Windows reserved device
    name (``con``, ``nul`` …) — unreadable as a file there, silently
    degrading the journal guarantee — and on a case-insensitive filesystem
    ``Op-1`` / ``op-1`` would share one journal (one operation replaying
    another's outcome). Hashing sidesteps both; the real id lives inside the
    record body, so replay still keys on it.
    """
    return operations_dir(project_dir) / f"{sha256(operation_id)}.json"


def read_operation(project_dir: str | Path, operation_id: str) -> dict[str, Any] | None:
    """The recorded outcome for ``operation_id`` or ``None`` (never started).

    An unreadable/corrupt body is NOT "never started": the attempt left an
    untrustworthy file, so the outcome is unknown and surfaces as
    recovery_required (never a blind replay).

    TODO-0227 REVIEW F1: the same managed bar as the writer. A symlinked
    ``.agentic`` / ``state`` / ``operations`` component is an unknown
    outcome too — ``is_file()`` follows the link, so a foreign record could
    be read as this project's ``applied``. The preflight runs BEFORE the
    existence check; missing components are fine (nothing was ever written).
    """
    path = op_path(project_dir, operation_id)
    try:
        managed_fs.assert_no_symlink_escape(
            path, root=managed_fs.agentic_root_for(path)
        )
    except managed_fs.ManagedPathError as e:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal path is not managed ({e}) — "
            "outcome unknown, recovery_required, do not retry blindly",
        ) from e
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal unreadable ({e}) — outcome "
            "unknown, recovery_required, do not retry blindly",
        ) from e
    return parse(text, operation_id=operation_id)


def write_operation(project_dir: str | Path, operation_id: str, **fields: Any) -> None:
    """Persist one journal transition. Raises on failure — the journal IS the
    durable guarantee, not a log line; a swallowed OSError would either lose
    a ``reserved`` record (replay re-applies) or strand one with no finish.

    TODO-0227 (M3): the record BODY is built by the one schema module
    (:mod:`._journal`) — status vocabulary, ``ts`` format, ``args_digest``
    and the exact serialized bytes are shared with the hygiene writer
    instead of being re-spelled here.
    """
    record = build_record(operation_id, **fields)
    atomic_write_json(op_path(project_dir, operation_id), record)


__all__ = ["validate_operation_id", "op_path", "read_operation", "write_operation"]
