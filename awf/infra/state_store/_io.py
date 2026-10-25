"""R1.3: paths, constants and the durable atomic publish primitive."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ... import paths
from .. import managed_fs

SCHEMA_VERSION = 2
CONTROL_FILE_NAME = "control.json"


def control_path(project_dir: str | Path) -> Path:
    return paths.state_control_file(project_dir)


def records_dir(project_dir: str | Path) -> Path:
    return paths.state_records_dir(project_dir)


def operations_dir(project_dir: str | Path) -> Path:
    return paths.state_operations_dir(project_dir)


def manifest_path(project_dir: str | Path) -> Path:
    return paths.state_migration_manifest(project_dir)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_text(data: Any) -> str:
    """The published JSON form: sorted keys, ``indent=2``, trailing newline.

    One spelling of the on-disk bytes for every Store writer — callers that
    must produce the same text (the operation-journal schema) import this
    instead of repeating the ``json.dumps`` arguments next to it.
    """
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def atomic_write_json(path: Path, data: Any) -> None:
    """Publish ``data`` as JSON atomically, durably and under the managed bar.

    TODO-0227 REVIEW F1: this used to call
    :func:`awf._atomic.atomic_write_text` directly, so every Store JSON
    writer (control.json, records, the operation journal, the migration
    manifest) could follow a symlinked ``.agentic`` / ``state`` out of the
    project. It now goes through
    :func:`awf.infra.managed_fs.managed_write_text`: every component from
    the project root down to the leaf is opened ``O_NOFOLLOW`` and the temp
    file is created inside the verified directory, then ``renameat``-ed.
    A symlinked component is refused before a single byte lands; on a
    platform without the no-follow primitives the write is refused
    (fail-closed) instead of silently degrading.

    ``durable=True`` keeps the host-crash guarantee: the file is fsynced
    before the rename and the verified directory fd after it.
    """
    managed_fs.managed_write_text(
        path,
        json_text(data),
        root=managed_fs.agentic_root_for(path),
        durable=True,
    )


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
    "json_text",
    "atomic_write_json",
    "canonical_json",
    "sha256",
    "payload_digest",
]
