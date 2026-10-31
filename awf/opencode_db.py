"""Shared opencode.db probe + readers (K3, K4, D8).

One module owns the schema decision and the "recent models" SQL: the CLI
(``awf/api/model_check.py``) and the plugin
(``agent_workflow_ui/.../opencode_config.py``) import it instead of keeping
two copies that would drift (AC12).

The only schema awf 3 reads is v2 — ``session_v2`` / ``session_message``
(the v1 tables are gone, the data columns moved to JSON in
``session_message.data``). The v2 marker is ``session_v2``, not
``session_message``: a v1 database already has an empty
``session_message`` table (CTX3, D7). Anything else is ``none``: metrics
answer ``n/a`` with a reason, the session capture writes no id.

``V2_MODEL_FIELD`` is one of the two facts IMP1 measured on the real 2.2.3
binary, not a guess (D9, A7).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# IMP1 probe (opencode 2.2.3): `session_v2.model` holds
# ``{"id": "...", "providerID": "..."}``. Until a probe confirms a field the
# constant stays None and the reader refuses to guess (RR25).
V2_MODEL_FIELD: tuple[str, str] | None = ("session_v2", "model")

SCHEMA_V2 = "v2"
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
    """``v2`` when ``session_v2`` + ``session_message`` exist, else ``none`` (K3).

    The marker is the table pair, not ``session_message`` alone: a v1
    database already carries an empty ``session_message`` (CTX3, D7).
    """
    names = table_names(con)
    if {"session_v2", "session_message"} <= names:
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
    """Recently used model IDs from ``opencode.db``, v2 only (K3, AC12).

    Reads ``V2_MODEL_FIELD``. A v1 or empty base is an empty list and one
    stderr line — the CLI and the plugin both degrade, neither crashes.
    """
    path = Path(db_path)
    if not path.is_file():
        return []
    try:
        con = sqlite3.connect(str(path), timeout=3)
    except sqlite3.Error:
        return []
    try:
        # The model column only needs its own table: a partial v2 database
        # (session_v2 without session_message) still carries history, and
        # ``detect_schema`` is strict on purpose (it decides the metrics
        # reader, not this one).
        if "session_v2" not in table_names(con):
            # One fixed message for both "no v2 table" and "field unconfirmed"
            # (K3, T11): the callers only need to know that the list is empty.
            print(
                "моделей не найдено (v2: поле не подтверждено)",
                file=sys.stderr,
            )
            return []
        return _dedupe(_rows_v2(con, limit), limit)
    except sqlite3.Error as e:
        print(f"model_check: cannot read opencode.db: {e}", file=sys.stderr)
        return []
    finally:
        con.close()


def available_models(timeout: float = _MODELS_TIMEOUT) -> list[str]:
    """``opencode models`` output as ``provider/model`` lines (R2).

    awf 3 runs opencode 2.x only, so the CLI gets ``--standalone`` on every
    call: without it the call goes to the shared server and can hang on a
    server the worker does not own. Any failure is an empty list and a
    warning, never an exception.
    """
    from . import opencode_version

    argv = [opencode_version.BINARY, "models"]
    if not opencode_version.is_managed(None):
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
    "SCHEMA_NONE",
    "SCHEMA_V2",
    "V2_MODEL_FIELD",
    "available_models",
    "detect_schema",
    "parse_model_id",
    "recent_models",
    "table_names",
]
