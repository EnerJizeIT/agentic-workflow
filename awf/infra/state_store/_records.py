"""R1.3: immutable, digest-addressed records + reachability GC."""
from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from ._errors import StateStoreError
from ._io import atomic_write_json, payload_digest, records_dir
from ._lock import project_lock
from ._snapshot import read_snapshot

# ``kind`` is PUBLIC input (``put_record``/``get_record`` and the keys of
# ``transact(records=...)``) and lands in the record path verbatim, so it is
# restricted to a safe slug. Hashing it (as ``_ops.op_path`` does for the
# operation id) would rename every on-disk record directory; a strict slug
# keeps the readable layout while making a traversal token impossible.
_KIND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_kind(kind: str) -> None:
    if not _KIND_RE.match(kind or ""):
        raise StateStoreError(
            f"invalid record kind {kind!r} — letters, digits, '.', '_', '-' "
            "only (1..64 chars, starts alphanumeric); it names a directory "
            "under state/records/ and is never a path traversal"
        )


def put_record(project_dir: str | Path, kind: str, payload: Any) -> str:
    """Publish an immutable record, addressed by its payload digest.

    Idempotent: the same payload yields the same digest and the same file.
    Records are only REACHABLE once a snapshot references the digest, so a
    record written before a crash is garbage, not success.
    """
    validate_kind(kind)
    digest = payload_digest(payload)
    path = records_dir(project_dir) / kind / f"{digest}.json"
    if not path.exists():
        atomic_write_json(path, payload)
    return digest


def get_record(
    project_dir: str | Path, digest: str, kind: str | None = None
) -> dict[str, Any] | None:
    """Read a record by digest, verifying the content still hashes to it."""
    if kind is not None:
        validate_kind(kind)
        candidates = [records_dir(project_dir) / kind / f"{digest}.json"]
    else:
        candidates = list(records_dir(project_dir).glob(f"*/{digest}.json"))
    if not candidates:
        return None
    path = candidates[0]
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if payload_digest(payload) != digest:
        return None
    return payload


def gc_records(project_dir: str | Path, *, dry_run: bool = True) -> list[str]:
    """Remove record files unreachable from the current snapshot.

    Reachability is by digest reference AFTER retention; unreferenced
    payloads from a crash are garbage, never treated as success.

    Runs under the project lock: a record published by a live ``transact``
    is only reachable once that transaction commits, so a GC that read the
    snapshot outside the lock could delete a record the same instant it was
    published. Never call this while already holding the lock.
    """
    with project_lock(project_dir):
        snap = read_snapshot(project_dir)
        if not snap.valid:
            return []
        reachable = {
            ref.get("digest")
            for ref in snap.data.get("records", [])
            if isinstance(ref, dict)
        }
        removed: list[str] = []
        for path in records_dir(project_dir).glob("*/*.json"):
            if path.stem in reachable:
                continue
            removed.append(str(path))
            if not dry_run:
                with contextlib.suppress(OSError):
                    path.unlink()
        return removed


__all__ = ["validate_kind", "put_record", "get_record", "gc_records"]
