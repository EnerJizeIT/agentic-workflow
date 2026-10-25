"""R1.3: state-store error taxonomy.

Each error carries the machine ``outcome`` the surfaces (MCP/CLI) expose
instead of a blind retry. All extend :class:`AwfApiError` so a surface can
convert any of them to ``{"status": "error", ...}`` without importing this
package.
"""
from __future__ import annotations

from typing import Literal

from ..._errors import AwfApiError

SnapshotStatus = Literal["valid", "missing", "corrupt", "unsupported"]


class StateStoreError(AwfApiError):
    """Base for state-store refusals."""

    outcome = "error"


class StateCorrupt(StateStoreError):
    """control.json exists but cannot be trusted — mutations are refused and
    the reader gets an explicit corrupt/unsupported status, never an empty
    project."""

    outcome = "refused"


class StateUnsupported(StateCorrupt):
    """The snapshot declares a schema this code cannot interpret — NEWER is
    unknown, OLDER is a legacy control.json; neither is auto-healed."""

    outcome = "refused"


class StaleState(StateStoreError):
    """The CAS precondition (expected revision) no longer matches — a newer
    generation published first. The snapshot is left untouched."""

    outcome = "stale_operation"


class OperationConflict(StateStoreError):
    """An operation id was reused with different arguments; the duplicate is
    refused rather than replayed under the wrong effect."""

    outcome = "refused"


class ReservationDenied(StateStoreError):
    """The requested action is forbidden while the current reservation
    matrix is in force (e.g. a second writer during a live worker)."""

    outcome = "refused"


class MigrationReconciliationRequired(StateStoreError):
    """The legacy state is ambiguous (conflicting run/current or
    "completed" inferred from a directory): migration refuses to guess."""

    outcome = "migration_requires_reconciliation"


__all__ = [
    "SnapshotStatus",
    "StateStoreError",
    "StateCorrupt",
    "StateUnsupported",
    "StaleState",
    "OperationConflict",
    "ReservationDenied",
    "MigrationReconciliationRequired",
]
