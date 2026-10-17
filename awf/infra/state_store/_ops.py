"""R1.3: the durable operation journal.

The PATH and KEY are shared with R1.2 ``hygiene`` (``api/hygiene.py``):
one ``state/operations/<sha256(id)>.json`` per operation id — a
user-supplied token is never a path component. The RECORD SCHEMA, however,
is Store-owned and DIFFERS from hygiene's: here the status vocabulary is
reserved/running/applied/refused/failed/recovery_required with an arguments
digest, while hygiene records started/refused/applied with a todo id. A
reader must key on the vocabulary it expects — reading hygiene's
``started`` as this module's ``reserved`` would be wrong. Unifying the two
vocabularies is deferred (R1.5); until then only the path and key are
common ground.

A retry reads the recorded outcome instead of guessing; an unreadable
record is unknown, never "never started".
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..._errors import OperationRecoveryRequired
from ._errors import StateStoreError
from ._io import atomic_write_json, operations_dir, sha256, timestamp

_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_operation_id(operation_id: str) -> None:
    if not _OPERATION_ID_RE.match(operation_id or ""):
        raise StateStoreError(
            f"invalid operation_id {operation_id!r} — letters, digits, '.', "
            "'_', '-' only (1..64 chars, starts alphanumeric)"
        )


def op_path(project_dir: str | Path, operation_id: str) -> Path:
    return operations_dir(project_dir) / f"{sha256(operation_id)}.json"


def read_operation(project_dir: str | Path, operation_id: str) -> dict[str, Any] | None:
    """The recorded outcome for ``operation_id`` or ``None`` (never started).

    An unreadable/corrupt body is NOT "never started": the attempt left an
    untrustworthy file, so the outcome is unknown and surfaces as
    recovery_required (never a blind replay).
    """
    path = op_path(project_dir, operation_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal unreadable ({e}) — outcome "
            "unknown, recovery_required, do not retry blindly",
        ) from e
    if not isinstance(data, dict) or data.get("operation_id") != operation_id:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal body does not name this id — "
            "outcome unknown, recovery_required, do not retry blindly",
        )
    return data


def write_operation(project_dir: str | Path, operation_id: str, **fields: Any) -> None:
    """Persist one journal transition. Raises on failure — the journal IS the
    durable guarantee, not a log line; a swallowed OSError would either lose
    a ``reserved`` record (replay re-applies) or strand one with no finish."""
    fields.setdefault("operation_id", operation_id)
    fields.setdefault("ts", timestamp())
    atomic_write_json(op_path(project_dir, operation_id), fields)


__all__ = ["validate_operation_id", "op_path", "read_operation", "write_operation"]
