"""Shared opencode.db probe + readers (K5, K6, D8).

One module owns the schema decision and the "recent models" SQL: the CLI
(``awf/api/model_check.py``) and the plugin
(``agent_workflow_ui/.../opencode_config.py``) import it instead of keeping
two copies that would drift (AC24).

Two database generations exist in the wild:

- v1 — ``session`` / ``part`` / ``message``;
- v2 — ``session_v2`` / ``session_message`` (``part`` is gone, the data
  columns moved to JSON in ``session_message.data``).

The v2 marker is ``session_v2``, not ``session_message``: a v1 database
already has an empty ``session_message`` table (CTX3, D7).

``V2_MODEL_FIELD`` and ``MODELS_STANDALONE_SUPPORTED`` are the two facts
IMP1 measured on the real 2.2.3 binary, not guesses (D9, A6, A7).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# IMP1 probe (opencode 2.2.3, 2026-10-30): `opencode models --standalone`
# is accepted and lists the same models (rc 0).
MODELS_STANDALONE_SUPPORTED = True

# IMP1 probe (opencode 2.2.3): `session_v2.model` holds
# ``{"id": "...", "providerID": "..."}``. Until a probe confirms a field the
# constant stays None and the reader refuses to guess (RR25).
V2_MODEL_FIELD: tuple[str, str] | None = ("session_v2", "model")

SCHEMA_V1 = "v1"
SCHEMA_V2 = "v2"
SCHEMA_BOTH = "both"
SCHEMA_NONE = "none"

_MODELS_TIMEOUT = 10.0


def table_names(con: sqlite3.Connection) -> set[str]:
    """Table names from ``sqlite_master``; empty on an unreadable DB."""
    try:
        return {
            str(row[0])
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except sqlite3.Error:
        return set()


def detect_schema(con: sqlite3.Connection) -> str:
    """``v1`` / ``v2`` / ``both`` / ``none`` by ``sqlite_master`` (K5)."""
    names = table_names(con)
    has_v1 = {"session", "part"} <= names
    has_v2 = {"session_v2", "session_message"} <= names
    if has_v1 and has_v2:
        return SCHEMA_BOTH
    if has_v1:
        return SCHEMA_V1
    if has_v2:
        return SCHEMA_V2
    return SCHEMA_NONE


def parse_model_id(raw: object) -> str | None:
    """``{"providerID","id"}`` → ``provider/id``; a plain string as is."""
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw or None
        return parse_model_id(parsed)
    if isinstance(raw, dict):
        provider = str(raw.get("providerID") or "")
        model = str(raw.get("id") or "")
        if provider and model:
            return f"{provider}/{model}"
        return model or None
    return None


def _limit_clause(limit: int | None) -> tuple[str, tuple[int, ...]]:
    if limit is None:
        return "", ()
    return " LIMIT ?", (limit * 2,)


def _rows_v1(con: sqlite3.Connection, limit: int | None) -> list[object]:
    tail, params = _limit_clause(limit)
    return [
        row[0]
        for row in con.execute(
            "SELECT model, MAX(time_created) AS last_used FROM session "
            "WHERE model IS NOT NULL GROUP BY model ORDER BY last_used DESC"
            + tail,
            params,
        ).fetchall()
    ]


def _rows_v2(con: sqlite3.Connection, limit: int | None) -> list[object]:
    if V2_MODEL_FIELD is None:
        print(
            "моделей не найдено (v2: поле не подтверждено)",
            file=sys.stderr,
        )
        return []
    table, column = V2_MODEL_FIELD
    tail, params = _limit_clause(limit)
    if column.startswith("$."):
        expr = f"json_extract(data, '{column}')"
        sql = (
            f"SELECT {expr}, MAX(time_created) AS last_used FROM {table} "
            f"WHERE {expr} IS NOT NULL GROUP BY {expr} "
            "ORDER BY last_used DESC" + tail
        )
    else:
        sql = (
            f"SELECT {column}, MAX(time_created) AS last_used FROM {table} "
            f"WHERE {column} IS NOT NULL GROUP BY {column} "
            "ORDER BY last_used DESC" + tail
        )
    return [row[0] for row in con.execute(sql, params).fetchall()]


def _dedupe(raw_rows: list[object], limit: int | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in raw_rows:
        model = parse_model_id(raw)
        if not model or model in seen:
            continue
        seen.add(model)
        out.append(model)
        if limit is not None and len(out) >= limit:
            break
    return out


def recent_models(db_path: str | Path, limit: int | None = 8) -> list[str]:
    """Recently used model IDs from ``opencode.db`` (K6, AC23).

    v1 — ``session.model``; v2 — ``V2_MODEL_FIELD``; both — union, v2 first,
    no duplicates. Any error degrades to an empty list.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect(str(path), timeout=3)
    except sqlite3.Error:
        return []
    try:
        # The model column only needs its own table: a partial v1 database
        # (session without part) still carries history, and ``detect_schema``
        # is strict on purpose (it decides the metrics reader, not this one).
        names = table_names(con)
        raw: list[object] = []
        if "session_v2" in names:
            raw += _rows_v2(con, limit)
        if "session" in names:
            raw += _rows_v1(con, limit)
        return _dedupe(raw, limit)
    except sqlite3.Error as e:
        print(f"model_check: cannot read opencode.db: {e}", file=sys.stderr)
        return []
    finally:
        con.close()


def available_models(timeout: float = _MODELS_TIMEOUT) -> list[str]:
    """``opencode models`` output as ``provider/model`` lines (K6, AC25–27).

    ``--standalone`` is added only for major ≥ 2 with a confirmed flag
    support; 1.x never gets it. Any failure is an empty list and a warning,
    never an exception.
    """
    from . import opencode_version

    argv = [opencode_version.BINARY, "models"]
    probe = opencode_version.probe_version()
    if (
        probe.status == "ok"
        and probe.version is not None
        and probe.version[0] >= 2
        and MODELS_STANDALONE_SUPPORTED
    ):
        argv.append("--standalone")
    try:
        import subprocess

        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (FileNotFoundError, OSError) as e:
        print(f"model_check: 'opencode models' CLI unavailable: {e}", file=sys.stderr)
        return []
    except Exception as e:  # noqa: BLE001 — CLI failure must never crash doctor
        print(f"model_check: 'opencode models' failed: {e}", file=sys.stderr)
        return []
    if result.returncode != 0:
        print(
            f"model_check: 'opencode models' exited {result.returncode}: "
            f"{(result.stderr or '').strip()[:200]}",
            file=sys.stderr,
        )
        return []
    if not result.stdout.strip():
        print("model_check: 'opencode models' вернул пустой список", file=sys.stderr)
        return []
    models: list[str] = []
    seen: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or "/" not in line or line.startswith("["):
            continue
        if line.startswith("page-assist"):
            continue
        if line not in seen:
            seen.add(line)
            models.append(line)
    return models


__all__ = [
    "MODELS_STANDALONE_SUPPORTED",
    "SCHEMA_BOTH",
    "SCHEMA_NONE",
    "SCHEMA_V1",
    "SCHEMA_V2",
    "V2_MODEL_FIELD",
    "available_models",
    "detect_schema",
    "parse_model_id",
    "recent_models",
    "table_names",
]
