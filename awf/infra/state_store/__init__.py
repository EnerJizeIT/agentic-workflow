"""R1.3 (roadmap R1): single owner of awf project state.

Before this package the project state was spread over ``state/run.yaml`` and
``state/current.yaml`` (both read-modify-written by many callers under an
advisory lock) plus a per-operation journal. Two writers could interleave,
``reset`` could delete state a live pipeline still owned, and a corrupt file
was indistinguishable from an empty project. This package is the one writer:

* ``state/control.json`` — ONE atomically published control snapshot
  (``schema_version`` 2) holding the active run, the execution, durable
  counters, the reservations matrix and REFERENCES to immutable records.
  It never holds logs, Markdown or history.
* ``state/records/<kind>/<digest>.json`` — immutable, digest-addressed
  payloads. Publishing the snapshot is the commit point; a record written
  before the crash is simply unreachable, not "success".
* ``state/operations/<sha>.json`` — the durable operation journal. The path
  and key (a digest of the operation id) are shared with R1.2 ``hygiene``,
  but the RECORD SCHEMA is Store-owned and differs from hygiene's
  (reserved/running/applied/refused/failed/recovery_required + an arguments
  digest, versus hygiene's started/refused/applied + a todo id) — see
  :mod:`._ops`. Only the path and key are common ground today; unifying the
  vocabularies is deferred to R1.5.
* ``state/migration-manifest.json`` — the migration manifest naming the
  legacy files. ``run.yaml`` / ``current.yaml`` are still WRITTEN by the
  run path in R1.4 — the Store owns the durable generation counter and the
  active-run witness, while the projections stay authoritative for the
  legacy readers. Converting them into read-only projections is the
  deferred end-state of R1.5 (same note as ``api/run.py::run_start``).

``transact`` takes the interprocess project lock, re-reads the snapshot,
checks the CAS precondition, publishes records, then atomically replaces the
snapshot (temp + fsync + replace + dir fsync). A long action (model, tests,
HTTP, human decision) does NOT hold the lock: it takes a durable
reservation/operation id, and finish checks ownership.
"""
from __future__ import annotations

from typing import NoReturn

from ..._errors import LockUnavailable, OperationRecoveryRequired
from .. import managed_fs
from ._errors import (
    MigrationReconciliationRequired,
    OperationConflict,
    OperationStatus,
    ReservationDenied,
    SnapshotStatus,
    StaleState,
    StateCorrupt,
    StateStoreError,
    StateUnsupported,
)
from ._generation import (
    active_run_generation,
    clear_active_run,
    current_run_generation,
    next_run_generation,
    publish_active_run,
    seed_run_generation,
)
from ._io import (
    CONTROL_FILE_NAME,
    SCHEMA_VERSION,
    atomic_write_json,
    canonical_json,
    control_path,
    manifest_path,
    operations_dir,
    payload_digest,
    records_dir,
    sha256,
    timestamp,
)
from ._lock import project_lock
from ._migration import main, migrate, migration_preview, read_projection
from ._ops import op_path, read_operation, validate_operation_id, write_operation
from ._projection import remove_state_file, write_state_file
from ._records import gc_records, get_record, put_record
from ._reservations import (
    action_permitted,
    check_action,
    release_reservation,
    reserve,
    takeover_reservation,
)
from ._snapshot import Snapshot, empty_snapshot, read_snapshot, require_usable
from ._transact import transact
from ._wipe import delete_state_tree

#: R2.5 REVIEW (iter 6, E1): the ONE catch-list of the "a state write/removal
#: cannot proceed" family. The Store is the single writer AND remover of
#: project state, so the family lives here instead of being re-spelled next to
#: each caller:
#:   * ``OSError`` — generic I/O and the lock's ``open`` / ``mkdir``;
#:   * ``ManagedPathError`` — the managed no-follow writer/remover refusing a
#:     symlinked ``.agentic`` / ``state`` (R1 carry a);
#:   * ``LockUnavailable`` — ``locked()`` refusing when neither ``fcntl``
#:     nor ``msvcrt`` exists (R1 carry b, the ONE "no flock" contract).
#:     The PRECISE member, not the ``AwfApiError`` base: the base also
#:     covers ``OperationRecoveryRequired`` — a "do not retry blindly"
#:     signal that must never degrade to "no state" (R2.5
#:     architector-review).
#: WRITERS swallow it (a state write is non-fatal — the pipeline degrades to
#: "no state"); REMOVERS cross the boundary as ``OSError`` (they PROPAGATE —
#: ``reset`` reports ``partial_failure`` — and every caller documents an
#: ``except OSError`` guard). One list, one place.
STATE_WRITE_FAILURES: tuple[type[BaseException], ...] = (
    OSError,
    managed_fs.ManagedPathError,
    LockUnavailable,
)


def raise_state_removal_failure(exc: BaseException, what: str) -> NoReturn:
    """Cross the state-removal boundary in the documented ``OSError`` family.

    A REMOVER propagates its refusal (``reset`` reports ``partial_failure``)
    and every caller guards ``except OSError`` — but the family
    (:data:`STATE_WRITE_FAILURES`) contains non-``OSError`` members. A
    ``ManagedPathError`` barrier or a ``LockUnavailable`` "no flock" therefore
    re-raises AS ``OSError`` with its cause, keeping the guard alive. ONE
    translation next to the ONE list, instead of a copy in each remover
    (``pipeline_state.clear_state`` / ``run_state.clear_run``) — R2.5
    architector-review.
    """
    if isinstance(exc, OSError):
        raise exc
    raise OSError(f"cannot {what}: {exc}") from exc

__all__ = [
    "SCHEMA_VERSION",
    "CONTROL_FILE_NAME",
    "SnapshotStatus",
    "OperationStatus",
    "StateStoreError",
    "StateCorrupt",
    "StateUnsupported",
    "StaleState",
    "OperationConflict",
    "OperationRecoveryRequired",
    "ReservationDenied",
    "MigrationReconciliationRequired",
    "STATE_WRITE_FAILURES",
    "raise_state_removal_failure",
    "Snapshot",
    "empty_snapshot",
    "read_snapshot",
    "require_usable",
    "project_lock",
    "transact",
    "control_path",
    "records_dir",
    "operations_dir",
    "manifest_path",
    "op_path",
    "atomic_write_json",
    "canonical_json",
    "payload_digest",
    "sha256",
    "timestamp",
    "validate_operation_id",
    "read_operation",
    "write_operation",
    "write_state_file",
    "remove_state_file",
    "delete_state_tree",
    "put_record",
    "get_record",
    "gc_records",
    "action_permitted",
    "check_action",
    "reserve",
    "release_reservation",
    "takeover_reservation",
    "read_projection",
    "active_run_generation",
    "current_run_generation",
    "next_run_generation",
    "publish_active_run",
    "seed_run_generation",
    "clear_active_run",
    "migration_preview",
    "migrate",
    "main",
]
