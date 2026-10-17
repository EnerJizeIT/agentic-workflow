"""R1.3: the control snapshot — one read, no repair, explicit status."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ._errors import SnapshotStatus, StateCorrupt, StateUnsupported
from ._io import SCHEMA_VERSION, control_path


def empty_snapshot() -> dict[str, Any]:
    """The valid starting state of a project that never had control.json.

    Missing is a distinct status from corrupt: only a genuinely absent file
    maps here, and the first ``transact`` publishes revision 1.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "run_generation": 0,
        "unit_revision": 0,
        "config_revision": 0,
        "active_run": None,
        "service_run": None,
        "execution": None,
        "counters": {"done": 0, "blocked": 0, "rejected": 0},
        "reservations": {},
        "records": [],
        "updated_at": None,
    }


@dataclass(frozen=True)
class Snapshot:
    """One read of the control snapshot. ``revision`` is the CAS token the
    caller must pass to ``transact`` unchanged."""

    status: SnapshotStatus
    revision: int
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def valid(self) -> bool:
        return self.status == "valid"


def read_snapshot(project_dir: str | Path) -> Snapshot:
    """Read control.json once. Never raises, never repairs.

    * missing      → revision 0, empty skeleton, usable for a first write
    * valid        → the parsed snapshot
    * corrupt      → unreadable / not an object / bad revision
    * unsupported  → schema_version is not SCHEMA_VERSION: NEWER is unknown,
      OLDER is a legacy control.json this code cannot safely read (neither is
      auto-healed — see the version branch below)
    """
    path = control_path(project_dir)
    if not path.is_file():
        return Snapshot("missing", 0, empty_snapshot())
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return Snapshot("corrupt", 0, {}, f"control.json unreadable: {e}")
    if not isinstance(data, dict):
        return Snapshot("corrupt", 0, {}, "control.json is not a JSON object")
    version = data.get("schema_version")
    if not isinstance(version, int):
        return Snapshot("corrupt", 0, {}, "control.json has no integer schema_version")
    if version != SCHEMA_VERSION:
        # Neither NEWER nor OLDER is "valid": a newer schema is unknown, and
        # an older one is a legacy control.json this code cannot safely
        # interpret. Reading an older one as valid would let ``migrate``
        # return ``no_change`` and silently keep the stale format forever
        # (the reader/guard mismatch). Both are refused, never auto-healed.
        where = "newer" if version > SCHEMA_VERSION else "older"
        return Snapshot(
            "unsupported",
            0,
            data,
            f"schema_version {version} is {where} than supported {SCHEMA_VERSION}",
        )
    revision = data.get("revision")
    if not isinstance(revision, int) or revision < 0:
        return Snapshot("corrupt", 0, {}, "control.json has no non-negative revision")
    return Snapshot("valid", revision, data)


def require_usable(snap: Snapshot, project_dir: str | Path) -> dict[str, Any]:
    """Turn a non-valid read into a refusal. A corrupt/unsupported snapshot
    must NEVER degrade to an empty project — that is the whole point."""
    if snap.status == "valid":
        return snap.data
    if snap.status == "missing":
        return empty_snapshot()
    if snap.status == "unsupported":
        raise StateUnsupported(
            f"{control_path(project_dir)}: {snap.error} — refusing to mutate; "
            "upgrade the tool or reconcile the snapshot explicitly"
        )
    raise StateCorrupt(
        f"{control_path(project_dir)}: {snap.error} — refusing to mutate; a "
        "corrupt snapshot is not an empty project (no silent fallback)"
    )


__all__ = ["Snapshot", "empty_snapshot", "read_snapshot", "require_usable"]
