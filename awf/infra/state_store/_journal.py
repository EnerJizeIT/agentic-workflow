"""Single owner of the operation-journal RECORD SCHEMA (TODO-0227, M3).

The path and key of one journal line are owned by :mod:`._ops`
(``state/operations/<sha256(id)>.json``); this module owns the BODY. Before
M3 there were two vocabularies on that one file: the Store's
reserved/running/applied/refused/failed/recovery_required with an arguments
digest, and hygiene's started/refused/applied with a todo id and its own
timestamp format. A reader had to know which writer produced a line — and
"started" read as "reserved" (or the reverse) is a wrong outcome.

Now BOTH writers build their record here:

* statuses — :data:`STATUSES` (the Store vocabulary). The legacy hygiene
  ``started`` is read as :data:`STATUS_RUNNING` (dual read: old files stay
  readable, they are never rewritten on read);
* ``ts`` — one :func:`._io.timestamp` format (UTC ISO 8601);
* ``args_digest`` — the operation arguments' digest in the one
  :func:`._io.payload_digest` form; the key is always present (``null`` for
  a writer whose transition has no arguments);
* bytes — :func:`._io.json_text` is THE serializer; there is no second
  wrapper next to it (the Store publisher uses it directly).

A record that matches neither vocabulary (unknown status, no id, not an
object) is an UNKNOWN outcome, never "never started" — :func:`parse` raises
:class:`~awf._errors.OperationRecoveryRequired`.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from ..._errors import OperationRecoveryRequired
from ._errors import StateStoreError
from ._io import timestamp

STATUS_RESERVED = "reserved"
STATUS_RUNNING = "running"
STATUS_APPLIED = "applied"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"
STATUS_RECOVERY_REQUIRED = "recovery_required"

#: The canonical vocabulary of one journal line.
STATUSES: frozenset[str] = frozenset(
    {
        STATUS_RESERVED,
        STATUS_RUNNING,
        STATUS_APPLIED,
        STATUS_REFUSED,
        STATUS_FAILED,
        STATUS_RECOVERY_REQUIRED,
    }
)

#: Typing mirror of :data:`STATUSES` (TODO-0227 REVIEW F3: this module is the
#: ONE owner — the duplicate ``Literal`` in ``_errors`` is gone). The
#: round-trip test asserts the two spellings agree, so a status added on one
#: side without the other is a red test, not silent drift.
OperationStatus = Literal[
    "reserved", "running", "applied", "refused", "failed", "recovery_required"
]

#: Legacy hygiene statuses readable from an older file (never written).
_LEGACY_STATUS = {"started": STATUS_RUNNING}


def canonical_status(status: str) -> str:
    """The unified status for ``status`` (legacy alias mapped)."""
    return _LEGACY_STATUS.get(status, status)


def build_record(
    operation_id: str,
    *,
    status: str,
    ts: str | None = None,
    args_digest: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build one canonical journal record.

    ``operation_id`` / ``status`` / ``ts`` / ``args_digest`` are owned here;
    ``fields`` are the transition's own facts (``todo_id``, ``action``,
    ``error``, ``revision_before`` …). The caller computes ``args_digest``
    with :func:`._io.payload_digest` when its transition has arguments.
    """
    canonical = canonical_status(status)
    if canonical not in STATUSES:
        raise StateStoreError(
            f"invalid operation status {status!r} — expected one of "
            f"{', '.join(sorted(STATUSES))}"
        )
    record: dict[str, Any] = {
        "operation_id": operation_id,
        "status": canonical,
        "ts": ts or timestamp(),
        "args_digest": args_digest,
    }
    record.update(fields)
    return record


def parse(text: str, *, operation_id: str) -> dict[str, Any]:
    """Parse a journal line, or raise ``OperationRecoveryRequired``.

    Dual read: a legacy hygiene ``started`` line is accepted and returned as
    ``running``. Anything that does not name this id or carry a known status
    is a foreign/corrupt format — an unknown outcome, never "never started".
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as e:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal unreadable ({e}) — outcome "
            "unknown, recovery_required, do not retry blindly",
        ) from e
    if not isinstance(data, dict):
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal is not an object — outcome "
            "unknown, recovery_required, do not retry blindly",
        )
    if data.get("operation_id") != operation_id:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal body does not name this id "
            f"({data.get('operation_id')!r}) — outcome unknown, "
            "recovery_required, do not retry blindly",
        )
    status = data.get("status")
    if not isinstance(status, str) or canonical_status(status) not in STATUSES:
        raise OperationRecoveryRequired(
            operation_id,
            f"operation {operation_id}: journal status {status!r} is not a "
            "known transition — outcome unknown, recovery_required, do not "
            "retry blindly",
        )
    data["status"] = canonical_status(status)
    return data


__all__ = [
    "STATUS_RESERVED",
    "STATUS_RUNNING",
    "STATUS_APPLIED",
    "STATUS_REFUSED",
    "STATUS_FAILED",
    "STATUS_RECOVERY_REQUIRED",
    "STATUSES",
    "OperationStatus",
    "canonical_status",
    "build_record",
    "parse",
]
