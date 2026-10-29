"""R6/R7 (IMP5): session identity, capture window and reuse policy.

T13–T20 from ``docs/spec/context-efficiency/context-efficiency.test-spec.md``:
the pair file, the attempt counter, the title/window capture on fake v1 and
v2 schemas, the dead/foreign/slow base, the kill-before-capture counter, and
the ``off``/``fix-only``/``always`` decision table. Every base is a temporary
sqlite file; the real opencode.db is never opened.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from awf import agent_stage, session_store
from awf.infra import managed_fs
from awf.pipeline import Stage

ROLE = "agent-implementer"
STAGE = "agent-developer"
TODO = "TODO-0001"
HOME_DIR = "/home/awf"

_SCHEMA = (
    "CREATE TABLE {table} (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
    "directory TEXT NOT NULL DEFAULT '', time_created INTEGER, "
    "time_updated INTEGER)"
)


def _make_db(path, table="session"):
    con = sqlite3.connect(path)
    con.execute(_SCHEMA.format(table=table))
    con.commit()
    con.close()
    return path


def _insert(db, sid, title, tc, *, directory=HOME_DIR, tu=None, table="session"):
    con = sqlite3.connect(db)
    con.execute(
        f"INSERT INTO {table} VALUES (?,?,?,?,?)", (sid, title, directory, tc, tu)
    )
    con.commit()
    con.close()


def _project(tmp_path):
    proj = tmp_path / "proj"
    for sub in ("inbox", "outbox", "logs", "roles", "context", "state"):
        (proj / ".agentic" / sub).mkdir(parents=True, exist_ok=True)
    (proj / ".agentic" / "roles" / f"{ROLE}.md").write_text("# role\n", encoding="utf-8")
    (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text("# TODO\n", encoding="utf-8")
    return proj


def _stage(name=STAGE, role=ROLE):
    return Stage(name=name, role=role, kind="execute")


def _runner(monkeypatch, db=None, *, insert=True, returncode=0):
    """Fake spawn: records each argv and (like opencode) inserts a session row."""
    seen: list[list[str]] = []

    def fake_run(cmd, cwd, watch_paths=None, watch_new_glob=None, logs_dir=None,
                 hard_timeout=None, env=None, signal_holder=None, **kwargs):
        seen.append(list(cmd))
        title = cmd[cmd.index("--title") + 1]
        # Like opencode: `--session` continues an existing session and does
        # not create a new row, so a reused entry has nothing to capture.
        if db is not None and insert and "--session" not in cmd:
            _insert(db, f"s{len(seen)}", title, int(time.time() * 1000))

        rc = returncode

        class _R:
            returncode = rc

        return _R()

    monkeypatch.setattr(
        "awf.signal_watch.run_subprocess_until_signal", fake_run, raising=True
    )
    monkeypatch.setattr(agent_stage, "collect_handoff", lambda *a, **kw: None)
    monkeypatch.setattr(agent_stage, "clean_stage_signals", lambda *a, **kw: None)
    return seen


def _config(mode="fix-only", model=None):
    cfg = {"engine": {"reuse_session": mode}}
    if model:
        cfg["models"] = {ROLE: {"model": model}}
    return cfg


def _write_pair(proj, *, session_id="s1", attempt=None, ts=None, agent=ROLE,
                model="", title=None, stage=STAGE):
    data = {
        "session_id": session_id,
        "title": title or f"awf-{agent}-{stage}-{TODO}-a{attempt or 1}",
        "agent": agent,
        "model": model,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
    }
    if attempt is not None:
        data["attempt"] = attempt
    path = session_store.session_file(proj, TODO, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(proj, monkeypatch, db, config, stage=None, attempt=1):
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    agent_stage.run_agent_stage(
        stage or _stage(), TODO, proj, config, proj / ".agentic" / "logs",
        attempt=attempt,
    )


def _log_text(proj):
    logs = list((proj / ".agentic" / "logs").glob("*"))
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in logs)


# T13 ----------------------------------------------------------------------
def test_title_pair_file_and_attempt_counter(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    seen = _runner(monkeypatch, db)

    _run(proj, monkeypatch, db, _config("fix-only"))
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    assert seen[0][seen[0].index("--title") + 1] == title
    pair = session_store.read_session_file(proj, TODO, STAGE)
    assert pair is not None
    assert set(pair) == {"session_id", "title", "agent", "model", "attempt", "ts"}
    assert pair["attempt"] == 1
    assert pair["session_id"] == "s1"
    assert pair["ts"]  # ISO-8601 UTC from the base row

    # Rollback + continue: the next engine invocation starts worker_run at 1
    # again, but the pair counter is in state and gives -a2 (AC21).
    _run(proj, monkeypatch, db, _config("fix-only"), attempt=1)
    assert seen[1][seen[1].index("--title") + 1] == (
        f"awf-{ROLE}-{STAGE}-{TODO}-a2"
    )
    assert seen[1][seen[1].index("--session") + 1] == "s1"
    pair2 = session_store.read_session_file(proj, TODO, STAGE)
    assert pair2["attempt"] == 2
    assert pair2["session_id"] == "s1"


def test_off_keeps_old_title_and_writes_no_file(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    seen = _runner(monkeypatch, db)
    monkeypatch.setattr(
        session_store, "_read",
        lambda *a, **kw: pytest.fail("off must not read the base"),
    )

    _run(proj, monkeypatch, db, _config("off"))

    assert seen[0][seen[0].index("--title") + 1] == f"awf-{ROLE}-{TODO}"
    assert "--session" not in seen[0]
    assert not session_store.session_file(proj, TODO, STAGE).exists()


# T14 ----------------------------------------------------------------------
@pytest.mark.parametrize("table", ["session", "session_v2"])
def test_capture_by_title_and_time_window(tmp_path, table):
    db = _make_db(tmp_path / "opencode.db", table)
    proj = tmp_path / "proj"
    proj.mkdir()
    now = int(time.time() * 1000)
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    other = f"awf-{ROLE}-agent-qa-{TODO}-a1"

    _insert(db, "a", title, now + 500, table=table)
    _insert(db, "b", title, now - 60_000, table=table)
    _insert(db, "c", f"awf-{ROLE}-other-{TODO}-a1", now + 500, table=table)
    _insert(db, "d", other, now + 500, directory="", table=table)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    try:
        sid = session_store.capture_after_spawn(
            proj, todo_id=TODO, stage=STAGE, title=title, agent=ROLE,
            model="", attempt=1, spawn_started_ms=now,
        )
        assert sid == "a"
        pair = session_store.read_session_file(proj, TODO, STAGE)
        assert pair["session_id"] == "a"

        sid2 = session_store.capture_after_spawn(
            proj, todo_id=TODO, stage="agent-qa", title=other, agent=ROLE,
            model="", attempt=1, spawn_started_ms=now,
        )
        assert sid2 == "d"
        assert session_store.read_session_file(proj, TODO, "agent-qa")["session_id"] == "d"

        # Directory is only a tie-break: same time_created, project dir wins
        # over $HOME even when the id would lose the id-DESC order.
        _insert(db, "zzz", title, now + 600, table=table)
        _insert(db, "aaa", title, now + 600, directory=str(proj), table=table)
        sid3 = session_store.capture_after_spawn(
            proj, todo_id="TODO-0002", stage=STAGE, title=title, agent=ROLE,
            model="", attempt=1, spawn_started_ms=now,
        )
        assert sid3 == "aaa"

        # Nothing in the window — no id, no file.
        assert session_store.capture_after_spawn(
            proj, todo_id="TODO-0003", stage=STAGE, title="awf-x-y-TODO-0003-a1",
            agent=ROLE, model="", attempt=1, spawn_started_ms=now + 300_000,
        ) is None
        assert not session_store.session_file(proj, "TODO-0003", STAGE).exists()
    finally:
        monkeypatch.undo()


# T15 ----------------------------------------------------------------------
def test_missing_base_writes_no_file_and_logs_reason(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _runner(monkeypatch, insert=False)

    _run(proj, monkeypatch, tmp_path / "no-such.db", _config("fix-only"))

    assert not session_store.session_file(proj, TODO, STAGE).exists()
    assert "session capture skipped" in _log_text(proj)


def test_foreign_schema_writes_no_file(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = tmp_path / "other.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other (id TEXT)")
    con.commit()
    con.close()
    _runner(monkeypatch, insert=False)

    _run(proj, monkeypatch, db, _config("fix-only"))

    assert not session_store.session_file(proj, TODO, STAGE).exists()
    assert "session capture skipped" in _log_text(proj)


def test_slow_base_respects_one_second_budget(tmp_path, monkeypatch):
    """T15/AUD-0303-1: the production budget is 1s; the test ceiling is 2s.

    The old ``< 3.0`` bound let a read twice the documented budget pass.
    """
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    holder = sqlite3.connect(db)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        started = time.monotonic()
        result = session_store.capture_after_spawn(
            proj, todo_id=TODO, stage=STAGE,
            title=f"awf-{ROLE}-{STAGE}-{TODO}-a1", agent=ROLE, model="",
            attempt=1, spawn_started_ms=int(time.time() * 1000),
        )
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert result is None
    assert session_store.DB_TIMEOUT <= 2.0
    assert elapsed < 2.0
    assert not session_store.session_file(proj, TODO, STAGE).exists()


def test_continuation_with_dead_base_keeps_previous_file(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    path = _write_pair(proj, session_id="keep", attempt=1)
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(session_store, "db_path", lambda: tmp_path / "gone.db")

    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    )
    assert decision.reused is False
    assert decision.reason == "session not found"
    assert path.read_text(encoding="utf-8") == before


# T16 ----------------------------------------------------------------------
def test_kill_before_capture_moves_counter(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    seen = _runner(monkeypatch, db, insert=False, returncode=1)
    with pytest.raises(RuntimeError):
        _run(proj, monkeypatch, db, _config("fix-only"))
    assert seen[0][seen[0].index("--title") + 1] == (
        f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    )
    assert not session_store.session_file(proj, TODO, STAGE).exists()

    seen2 = _runner(monkeypatch, db)
    _run(proj, monkeypatch, db, _config("fix-only"), attempt=1)
    assert seen2[0][seen2[0].index("--title") + 1] == (
        f"awf-{ROLE}-{STAGE}-{TODO}-a2"
    )
    pair = session_store.read_session_file(proj, TODO, STAGE)
    assert pair["attempt"] == 2


# T18 ----------------------------------------------------------------------
def test_fix_only_chooses_by_entry_number(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    _write_pair(proj, session_id="s1", attempt=1)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    first = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("fix-only"),
    )
    assert first.reused is False and first.reason == "attempt 1"

    # The TODO body (a `## REVIEW-фикс` section included) is not a condition.
    (proj / ".agentic" / "inbox" / f"{TODO}.md").write_text(
        "# TODO\n\n## REVIEW-фикс\n\nfix it\n", encoding="utf-8"
    )
    second = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    )
    assert second.reused is True and second.session_id == "s1"


def test_off_decision_never_touches_base(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setattr(
        session_store, "_read",
        lambda *a, **kw: pytest.fail("off must not read the base"),
    )
    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("off"),
    )
    assert decision.reused is False
    assert decision.reason == "reuse_session off"


# T19 ----------------------------------------------------------------------
def test_always_role_gate_and_pair_isolation(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role="agent-qa-review",
        agent="agent-qa-review", model="", attempt=2,
        config=_config("always"),
    ).reason == "role not allowed"

    # Another stage / another unit is never taken.
    _write_pair(proj, stage=STAGE)
    assert session_store.decide(
        proj, todo_id=TODO, stage="agent-qa", role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("always"),
    ).reason == "no session file"
    assert session_store.decide(
        proj, todo_id="TODO-0009", stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("always"),
    ).reason == "no session file"


def test_always_lost_counter_still_reuses(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    _write_pair(proj, session_id="s1", attempt=None)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    always = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=3, config=_config("always"),
    )
    assert always.reused is True
    fix_only = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("fix-only"),
    )
    assert fix_only.reused is False and fix_only.reason == "attempt 1"


# T20 ----------------------------------------------------------------------
def test_mismatches_and_age_give_fresh(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    _write_pair(proj, session_id="s1", attempt=1, model="other/model")
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "role/model mismatch"

    old = datetime.now(timezone.utc) - timedelta(minutes=241)
    _write_pair(proj, session_id="s1", attempt=1, ts=old.isoformat())
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "session too old"

    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    _write_pair(proj, session_id="s1", attempt=1, ts=future.isoformat())
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "session too old"

    session_store.session_file(proj, TODO, STAGE).write_text("{not json", encoding="utf-8")
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "no session file"

    _write_pair(proj, session_id="missing", attempt=1)
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "session not found"

    _write_pair(proj, session_id="s1", attempt=1)
    control = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    )
    assert control.reused is True


def test_reused_argv_keeps_flags_and_files(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    seen = _runner(monkeypatch, db)
    cfg = _config("fix-only", model="test/model")

    _run(proj, monkeypatch, db, cfg, attempt=1)
    first = seen[0]
    _run(proj, monkeypatch, db, cfg, attempt=1)
    second = seen[1]

    assert "--agent" in second and second[second.index("--agent") + 1] == ROLE
    assert second[second.index("--model") + 1] == "test/model"
    first_files = [second[i + 1] for i, tok in enumerate(second) if tok == "--file"]
    for value in (first[i + 1] for i, tok in enumerate(first) if tok == "--file"):
        assert value in first_files
    assert second.index("--session") == second.index("--model") + 2
    assert second[second.index("--session") + 1] == "s1"


# AC21: the counter survives kill + continue ---------------------------------
def test_attempt_counter_survives_kill_and_continue(tmp_path):
    from awf.api import pipeline as api_pipeline
    from awf.pipeline_state import bump_stage_attempt, read_state

    proj = tmp_path / "proj"
    (proj / ".agentic" / "state").mkdir(parents=True)
    assert bump_stage_attempt(proj, TODO, STAGE) == 1
    api_pipeline._clear_state_keep_salvage(proj)
    state = read_state(proj) or {}
    assert state.get("stage_attempts") == {f"{TODO}:{STAGE}": 1}
    assert bump_stage_attempt(proj, TODO, STAGE) == 2


# K6/D13: the pair keys are path components — a traversal or a separator in
# the stage must never reach the reader or the write path.
def test_pair_keys_are_path_components(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", "awf-x", int(time.time() * 1000))
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    for bad in ("../escape", "a/b", "a\\b", ".."):
        with pytest.raises(managed_fs.ManagedPathError):
            session_store.session_file(proj, TODO, bad)
        assert session_store.read_session_file(proj, TODO, bad) is None

    assert session_store.capture_after_spawn(
        proj, todo_id=TODO, stage="../escape", title="awf-x", agent=ROLE,
        model="", attempt=1, spawn_started_ms=int(time.time() * 1000),
    ) is None
    assert not list((proj / ".agentic").rglob("SESSION-*"))


# T13: `ts` is the base row's time_updated, falling back to time_created.
def test_pair_file_ts_prefers_time_updated(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    now = int(time.time() * 1000)
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    _insert(db, "upd", title, now, tu=now + 5_000)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title=title, agent=ROLE, model="",
        attempt=1, spawn_started_ms=now,
    )
    expected = datetime.fromtimestamp((now + 5_000) / 1000, tz=timezone.utc).isoformat()
    assert session_store.read_session_file(proj, TODO, STAGE)["ts"] == expected

    # time_updated = 0 means "never updated": the creation time is the ts.
    (proj / ".agentic" / "context" / f"SESSION-{TODO}-{STAGE}.json").unlink()
    title2 = f"awf-{ROLE}-{STAGE}-TODO-0004-a1"
    _insert(db, "zero", title2, now, tu=0)
    session_store.capture_after_spawn(
        proj, todo_id="TODO-0004", stage=STAGE, title=title2, agent=ROLE, model="",
        attempt=1, spawn_started_ms=now,
    )
    zero = session_store.read_session_file(proj, "TODO-0004", STAGE)
    assert zero["session_id"] == "zero"
    assert zero["ts"] == datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat()


# T20: an unreadable/absent ts is a reason for a fresh session.
def test_missing_ts_gives_fresh(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    pair = _write_pair(proj, session_id="s1", attempt=1)
    data = json.loads(pair.read_text(encoding="utf-8"))
    data["ts"] = ""
    pair.write_text(json.dumps(data), encoding="utf-8")

    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    )
    assert decision.reused is False
    assert decision.reason == "session too old"
