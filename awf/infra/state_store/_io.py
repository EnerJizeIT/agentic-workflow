"""R1.3: paths, constants and the durable atomic publish primitive."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ... import paths
from ..._atomic import atomic_write_text

SCHEMA_VERSION = 2
CONTROL_FILE_NAME = "control.json"


def control_path(project_dir: str | Path) -> Path:
    return paths.state_dir(project_dir) / CONTROL_FILE_NAME


def records_dir(project_dir: str | Path) -> Path:
    return paths.state_dir(project_dir) / "records"


def operations_dir(project_dir: str | Path) -> Path:
    return paths.state_dir(project_dir) / "operations"


def manifest_path(project_dir: str | Path) -> Path:
    return paths.state_dir(project_dir) / "migration-manifest.json"


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_json(path: Path, data: Any) -> None:
    """Publish ``data`` as JSON atomically and durably.

    The temp+rename+fsync mechanics live in the shared
    :func:`awf._atomic.atomic_write_text` (``durable=True``) — one atomic
    writer in the codebase, not a second copy next to it. Supported on POSIX
    local filesystems; on Windows the dir-fsync branch is a no-op
    (``os.O_DIRECTORY`` absent) and durability degrades to
    process-crash-only.
    """
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    atomic_write_text(path, payload, durable=True)


def canonical_json(data: Any) -> str:
    """Stable serialization used for every digest. Keys sorted, compact
    separators, UTF-8 — so the same payload always hashes the same."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def payload_digest(payload: Any) -> str:
    return sha256(canonical_json(payload))


__all__ = [
    "SCHEMA_VERSION",
    "CONTROL_FILE_NAME",
    "control_path",
    "records_dir",
    "operations_dir",
    "manifest_path",
    "timestamp",
    "atomic_write_json",
    "canonical_json",
    "sha256",
    "payload_digest",
]
