"""R1.3: ``transact`` — the ONLY way to change the control snapshot.

Under the interprocess project lock:

1. re-read the snapshot (corrupt/unsupported → refusal, no fallback);
2. resolve the operation id before touching anything — ``applied`` replays
   the recorded result, ``refused`` re-raises, ``reserved``/``running``/
   ``recovery_required`` are refused as recovery_required, and ``failed``
   (written only pre-commit, so no effect landed) is RETRIED under the same
   args — see the branch below;
3. check the CAS precondition ``expected_revision`` (mismatch →
   ``StaleState``, snapshot untouched);
4. apply ``mutate`` to a copy — returning ``None`` is an explicit no-op:
   nothing is published and nothing is journaled (a release of an unheld
   reservation must not bump the revision or record a hollow ``applied``);
5. write the ``reserved`` journal record (raises BEFORE any effect);
6. publish immutable ``records`` (commit points for the payloads);
7. atomically publish revision+1;
8. write the ``applied`` journal record. A failure here means the effect
   landed but is unrecorded → ``OperationRecoveryRequired`` (no double
   effect on retry).

``mutate`` receives the current snapshot and returns the new snapshot body
(revision is managed here), or ``None`` for a no-op. Raising inside
``mutate`` publishes nothing: a :class:`StateStoreError` (a precondition
refusal) records ``refused``; any other exception records ``failed``.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..._errors import OperationRecoveryRequired
from ._errors import OperationConflict, StaleState, StateStoreError
from ._io import (
    SCHEMA_VERSION,
    atomic_write_json,
    canonical_json,
    control_path,
    payload_digest,
    timestamp,
)
from ._lock import project_lock
from ._ops import read_operation, validate_operation_id, write_operation
from ._records import put_record
from ._snapshot import read_snapshot, require_usable


def transact(
    project_dir: str | Path,
    *,
    expected_revision: int,
    operation_id: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    args: Any = None,
    records: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply one CAS transaction to the control snapshot; see module doc."""
    validate_operation_id(operation_id)
    records = dict(records or {})
    args_digest = None if args is None else payload_digest(args)

    with project_lock(project_dir):
        snap = read_snapshot(project_dir)
        base = require_usable(snap, project_dir)

        prior = read_operation(project_dir, operation_id)
        if prior is not None:
            recorded_args = prior.get("args_digest")
            if recorded_args != args_digest:
                raise OperationConflict(
                    f"operation {operation_id} already ran with different "
                    "arguments — refusing to replay it under a new effect"
                )
            status = prior.get("status")
            if status == "applied":
                return {
                    "revision": prior.get("revision_after", snap.revision),
                    "status": "applied",
                    "replayed": True,
                    "result": prior.get("result"),
                }
            if status == "refused":
                raise StateStoreError(
                    prior.get("error") or f"operation {operation_id} was refused"
                )
            if status != "failed":
                # reserved/running/recovery_required: started without a finish
                raise OperationRecoveryRequired(
                    operation_id,
                    f"operation {operation_id}: status {status!r} — an attempt "
                    "started and recorded no finish; recovery_required, do not "
                    "retry blindly (a retry could double the effect)",
                )
            # status == "failed": written ONLY from the mutate/publish phase,
            # before the atomic snapshot replace that is the commit point. A
            # fault there means ``os.replace`` never ran (see ``_atomic``), so
            # the effect did NOT land — a retry with the SAME arguments
            # (checked above) is safe and must be allowed, not replayed as the
            # stale failure forever. The resumed attempt overwrites this
            # durable ``failed`` line with ``reserved``.

        if snap.revision != expected_revision:
            raise StaleState(
                f"expected revision {expected_revision} but control.json is at "
                f"{snap.revision} — a newer generation published first; the "
                "snapshot was left untouched (re-read and retry)"
            )

        # Apply ``mutate`` BEFORE the ``reserved`` marker: an explicit no-op
        # (``None``) publishes nothing and writes no journal line, while a
        # refusal/fault from ``mutate`` is still journaled exactly as before.
        new = json.loads(canonical_json(base))
        if mutate is not None:
            try:
                produced = mutate(new)
                if produced is None:
                    return {
                        "revision": snap.revision,
                        "status": "no_change",
                        "replayed": False,
                    }
                if not isinstance(produced, dict):
                    raise StateStoreError("mutate must return a snapshot dict")
                new = produced
            except BaseException as e:
                status = "refused" if isinstance(e, StateStoreError) else "failed"
                write_operation(
                    project_dir,
                    operation_id,
                    status=status,
                    args_digest=args_digest,
                    error=str(e),
                )
                raise

        write_operation(
            project_dir,
            operation_id,
            status="reserved",
            args_digest=args_digest,
            revision_before=snap.revision,
        )

        try:
            refs: list[dict[str, Any]] = []
            for kind, payload in records.items():
                digest = put_record(project_dir, kind, payload)
                refs.append({"kind": kind, "digest": digest})

            new_revision = snap.revision + 1
            new["schema_version"] = SCHEMA_VERSION
            new["revision"] = new_revision
            new["updated_at"] = timestamp()
            new["records"] = list(new.get("records", [])) + refs

            atomic_write_json(control_path(project_dir), new)
            result = {"revision": new_revision}
        except BaseException as e:
            status = "refused" if isinstance(e, StateStoreError) else "failed"
            write_operation(
                project_dir,
                operation_id,
                status=status,
                args_digest=args_digest,
                error=str(e),
            )
            raise

        try:
            write_operation(
                project_dir,
                operation_id,
                status="applied",
                args_digest=args_digest,
                revision_after=new_revision,
                result=result,
            )
        except Exception as e:
            # T2b: the finish write runs AFTER the snapshot is published, so
            # ANY failure here — not only OSError — means the effect landed
            # while its outcome stays unrecorded. The contract "surface
            # returns recovery_required" must not rest on OSError alone;
            # catching Exception keeps a non-OSError write fault (a
            # serialization error, an injected fault) from escaping raw and
            # leaving the caller free to retry blindly.
            raise OperationRecoveryRequired(
                operation_id,
                f"operation {operation_id}: snapshot published but the finish "
                f"record failed ({e}) — recovery_required, the effect landed",
            ) from e

        return {
            "revision": new_revision,
            "status": "applied",
            "replayed": False,
            "result": result,
        }


__all__ = ["transact"]
