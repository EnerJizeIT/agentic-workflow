"""R6/R7: session identity file, database capture and reuse policy.

The worker of a pair (unit, stage) leaves ``SESSION-<todo>-<stage>.json``
with the opencode session id. A retried or continued entry may continue
that session instead of spawning a fresh one. The base is the opencode
sqlite database, opened read-only under a one-second budget: a missing or
unknown base never fails the stage — the id is simply not written and the
reason goes to the stage log.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_mod
from . import paths
from ._log import log as _log
from .infra import managed_fs

DB_TIMEOUT = 1.0
CAPTURE_EARLY_MS = 2_000
CAPTURE_LATE_MS = 120_000
DEFAULT_MAX_AGE_MINUTES = 240
ALLOWED_ROLES = frozenset(
    {
        "agent-implementer",
        "agent-refactoring-specialist",
        # IMP5 (TODO-0304): the checklist-check stage continues the QA
        # session of the unit's earlier QA stage.
        "agent-qa-review",
    }
)
MODES = ("off", "fix-only", "always")

_SESSION_TABLES = ("session", "session_v2")
_KEY_RE = re.compile(r"^[^/\\]+$")


class _DbError(Exception):
    """The base is unavailable, foreign or too slow — no id is written."""


@dataclass(frozen=True)
class Decision:
    reused: bool
    session_id: str
    reason: str


def db_path() -> Path:
    """Path of the opencode sqlite base (the metrics default)."""
    from .metrics import default_db_path

    return default_db_path()


def session_file(project_dir: Path, todo_id: str, stage: str) -> Path:
    """Pair file path; both keys become path components (K6).

    Validated here, at the single path builder: a traversal in the unit id or
    a stage id with a separator must never reach the writer or a reader.
    """
    managed_fs.validate_todo_id(todo_id)
    if not _KEY_RE.match(stage or "") or stage in (".", ".."):
        raise managed_fs.ManagedPathError(
            f"invalid stage key {stage!r} for SESSION-<todo>-<stage>.json"
        )
    return paths.context_dir(project_dir) / f"SESSION-{todo_id}-{stage}.json"


def session_title(role: str, stage: str, todo_id: str, attempt: int) -> str:
    return f"awf-{role}-{stage}-{todo_id}-a{attempt}"


def mode(config: dict) -> str:
    value = config_mod.get(config, "engine.reuse_session", "off")
    if not isinstance(value, str):
        return "off"
    value = value.strip().lower()
    return value if value in MODES else "off"


def decision_line(decision: Decision) -> str:
    if decision.reused:
        return f"session: reused {decision.session_id}"
    return f"session: fresh ({decision.reason})"


def _max_age_minutes(config: dict) -> int:
    value = config_mod.get(
        config, "engine.session_max_age_minutes", DEFAULT_MAX_AGE_MINUTES
    )
    try:
        return int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_MINUTES


def _iso(ms: int | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()


def _age_minutes(ts: object) -> float | None:
    """Age of an ISO-8601 ``ts`` in minutes; None when it is unreadable/future."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if parsed > now:
        return None
    return (now - parsed).total_seconds() / 60.0


def read_session_file(
    project_dir: Path, todo_id: str, stage: str
) -> dict | None:
    """Parsed pair file, or None when absent/broken (K6: exactly six keys)."""
    try:
        path = session_file(project_dir, todo_id, stage)
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, managed_fs.ManagedPathError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("session_id"), str) or not data["session_id"]:
        return None
    return data


def _write_session_file(
    project_dir: Path,
    todo_id: str,
    stage: str,
    *,
    session_id: str,
    title: str,
    agent: str,
    model: str,
    attempt: int,
    ts: str,
) -> None:
    payload = {
        "session_id": session_id,
        "title": title,
        "agent": agent,
        "model": model,
        "attempt": attempt,
        "ts": ts,
    }
    managed_fs.managed_write_under_agentic(
        session_file(project_dir, todo_id, stage),
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _persist_row(
    project_dir: Path,
    todo_id: str,
    stage: str,
    *,
    session_id: str,
    title: str,
    agent: str,
    model: str,
    attempt: int,
    ts_ms: int | None,
    logs_dir: Path | None,
) -> str | None:
    """Write the pair file for a confirmed base row; a failed write is not fatal."""
    try:
        _write_session_file(
            project_dir, todo_id, stage,
            session_id=session_id, title=title, agent=agent, model=model,
            attempt=attempt, ts=_iso(ts_ms),
        )
    except (OSError, managed_fs.ManagedPathError) as e:
        if logs_dir is not None:
            _log(logs_dir, f"session capture skipped: write failed: {e}")
        return None
    return session_id


def _session_table(con: sqlite3.Connection) -> str | None:
    try:
        names = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    except sqlite3.Error as e:
        raise _DbError(f"schema unreadable: {e}") from e
    for name in _SESSION_TABLES:
        if name in names:
            return name
    return None


def _read(fn):
    """Run ``fn(con, table)`` against a read-only base under the 1s budget."""
    path = db_path()
    started = time.monotonic()
    result = None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            table = _session_table(con)
            if table is None:
                raise _DbError("unknown schema")
            result = fn(con, table)
        finally:
            con.close()
    except (sqlite3.Error, OSError) as e:
        raise _DbError(str(e)) from e
    if time.monotonic() - started > DB_TIMEOUT:
        raise _DbError("read exceeded 1s")
    return result


def _row_by_id(con: sqlite3.Connection, table: str, session_id: str):
    try:
        return con.execute(
            "SELECT id, CASE WHEN time_updated IS NULL OR time_updated = 0 "
            f"THEN time_created ELSE time_updated END FROM {table} WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error as e:
        raise _DbError(str(e)) from e


def _row_by_title(
    con: sqlite3.Connection,
    table: str,
    title: str,
    spawn_started_ms: int,
    now_ms: int,
    project_dir: Path,
):
    try:
        return con.execute(
            "SELECT id, CASE WHEN time_updated IS NULL OR time_updated = 0 "
            "THEN time_created ELSE time_updated END "
            f"FROM {table} WHERE title = ? AND time_created >= ? "
            "AND time_created <= ? "
            "ORDER BY time_created DESC, "
            "(CASE WHEN directory = ? THEN 0 ELSE 1 END), id DESC LIMIT 1",
            (title, spawn_started_ms - CAPTURE_EARLY_MS, now_ms + CAPTURE_LATE_MS,
             str(project_dir)),
        ).fetchone()
    except sqlite3.Error as e:
        raise _DbError(str(e)) from e


def decide(
    project_dir: Path,
    *,
    todo_id: str,
    stage: str,
    role: str,
    agent: str,
    model: str,
    attempt: int,
    config: dict,
    logs_dir: Path | None = None,
) -> Decision:
    """K7: choose reused/fresh for this entry, with the exact reason."""
    current = mode(config)
    if current == "off":
        return Decision(False, "", "reuse_session off")
    if role not in ALLOWED_ROLES:
        return Decision(False, "", "role not allowed")

    data = read_session_file(project_dir, todo_id, stage)
    if data is None:
        return Decision(False, "", "no session file")
    if data.get("agent") != agent or data.get("model") != model:
        return Decision(False, "", "role/model mismatch")

    age = _age_minutes(data.get("ts"))
    if age is None or age > _max_age_minutes(config):
        return Decision(False, "", "session too old")

    session_id = str(data["session_id"])
    try:
        row = _read(lambda con, table: _row_by_id(con, table, session_id))
    except _DbError as e:
        if logs_dir is not None:
            _log(logs_dir, f"session lookup skipped: {e}")
        return Decision(False, "", "session not found")
    if row is None:
        return Decision(False, "", "session not found")

    if current == "fix-only" and attempt <= 1:
        return Decision(False, "", "attempt 1")
    return Decision(True, session_id, "reused")


def capture_after_spawn(
    project_dir: Path,
    *,
    todo_id: str,
    stage: str,
    title: str,
    agent: str,
    model: str,
    attempt: int,
    spawn_started_ms: int,
    logs_dir: Path | None = None,
) -> str | None:
    """K6: find the fresh session by title+window and write the pair file."""
    now_ms = int(time.time() * 1000)
    try:
        row = _read(
            lambda con, table: _row_by_title(
                con, table, title, spawn_started_ms, now_ms, project_dir
            )
        )
    except _DbError as e:
        if logs_dir is not None:
            _log(logs_dir, f"session capture skipped: {e}")
        return None
    if row is None:
        if logs_dir is not None:
            _log(logs_dir, "session capture skipped: session not found")
        return None
    session_id, ts_ms = row
    return _persist_row(
        project_dir, todo_id, stage,
        session_id=session_id, title=title, agent=agent, model=model,
        attempt=attempt, ts_ms=ts_ms, logs_dir=logs_dir,
    )


def confirm_and_update(
    project_dir: Path,
    *,
    todo_id: str,
    stage: str,
    session_id: str,
    title: str,
    agent: str,
    model: str,
    attempt: int,
    logs_dir: Path | None = None,
) -> str | None:
    """K7: confirm the continued id in the base and refresh the pair file.

    A dead base keeps the previous file untouched (AC22, D6).
    """
    try:
        row = _read(lambda con, table: _row_by_id(con, table, session_id))
    except _DbError as e:
        if logs_dir is not None:
            _log(logs_dir, f"session capture skipped: {e}")
        return None
    if row is None:
        if logs_dir is not None:
            _log(logs_dir, "session capture skipped: session not found")
        return None
    _, ts_ms = row
    return _persist_row(
        project_dir, todo_id, stage,
        session_id=session_id, title=title, agent=agent, model=model,
        attempt=attempt, ts_ms=ts_ms, logs_dir=logs_dir,
    )


__all__ = [
    "ALLOWED_ROLES",
    "Decision",
    "CAPTURE_EARLY_MS",
    "CAPTURE_LATE_MS",
    "DEFAULT_MAX_AGE_MINUTES",
    "capture_after_spawn",
    "confirm_and_update",
    "db_path",
    "decide",
    "decision_line",
    "mode",
    "read_session_file",
    "session_file",
    "session_title",
]
