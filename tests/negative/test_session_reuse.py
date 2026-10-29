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
    assert "session capture: " in _log_text(proj)


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
    assert "session capture: " in _log_text(proj)


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
        proj, todo_id=TODO, stage=STAGE, role="agent-project-auditor",
        agent="agent-project-auditor", model="", attempt=2,
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

    # TODO-0306: the base is readable but the id is gone — a dead session.
    # The pair file is retired and the entry starts fresh.
    _write_pair(proj, session_id="missing", attempt=1)
    assert session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("fix-only"),
    ).reason == "session dead (opencode restart)"
    retired = session_store.read_session_file(proj, TODO, STAGE)
    assert retired["session_id"] == "missing"
    assert retired["status"] == "retired"

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


# TODO-0306 (PA-0306-4): ``on`` is the spec's "enabled" policy — a fresh
# first entry and the same session on a retry — NOT ``always``.
def test_on_mode_is_the_spec_policy_not_always(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    _write_pair(proj, session_id="s1", attempt=1)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    # ``on`` is a config alias, not a policy — mode() resolves it at the
    # boundary, and the policy is ``fix-only``, so the first entry is fresh
    # and the retry continues the pair. ``always`` stays separate.
    assert "on" in session_store.MODES
    assert session_store.mode(_config("on")) == "fix-only"
    assert session_store.mode(_config("always")) == "always"
    first = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("on"),
    )
    assert first.reused is False and first.reason == "attempt 1"
    retry = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("on"),
    )
    assert retry.reused is True and retry.session_id == "s1"


def test_always_mode_reuses_the_first_entry(tmp_path, monkeypatch):
    """PA-0306-4: ``always`` is the one policy that continues even when the
    pair's attempt counter is 1 — kept apart from ``on``/``fix-only``."""
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _insert(db, "s1", f"awf-{ROLE}-{STAGE}-{TODO}-a1", int(time.time() * 1000))
    _write_pair(proj, session_id="s1", attempt=1)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("always"),
    )
    assert decision.reused is True and decision.session_id == "s1"


# TODO-0306 (3): a dead session never hangs the stage — fresh + reason.
def test_dead_session_spawns_fresh_and_logs_reason(tmp_path, monkeypatch):
    """The pair names an id the base no longer has (opencode restart): the
    entry starts a FRESH session (no ``--session``), the stage log carries
    the reason, and the pair file is marked ``status: retired``."""
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _write_pair(proj, session_id="gone", attempt=1)
    seen = _runner(monkeypatch, db)

    _run(proj, monkeypatch, db, _config("fix-only"))

    assert "--session" not in seen[0], seen[0]
    assert "session dead (opencode restart)" in _log_text(proj)
    # The fake spawn created a new row, so the pair file is fresh again —
    # the retirement was written before the spawn (and overwritten by the
    # capture); the log is the durable evidence of the reason.
    pair = session_store.read_session_file(proj, TODO, STAGE)
    assert pair is not None and pair["session_id"] != "gone"


# TODO-0306 (QA): the retire write is best effort — a broken disk logs, the
# stage still starts fresh instead of falling.
def test_retire_write_failure_never_falls_the_stage(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_db(tmp_path / "opencode.db")
    _write_pair(proj, session_id="gone", attempt=1)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session_store, "_write_payload", _boom)

    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=1, config=_config("on"),
        logs_dir=proj / ".agentic" / "logs",
    )

    assert decision.reused is False
    assert decision.reason == "session dead (opencode restart)"
    assert "session retire skipped" in _log_text(proj)


# TODO-0306 (QA): нечитаемая база — не смерть сессии. Retire-маркер ставится
# только когда база прочитана и id в ней реально нет; иначе сбой диска/схемы
# пометил бы живую сессию мёртвой и потерял бы её для следующего витка.
def test_unreadable_base_does_not_retire_the_pair(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    _write_pair(proj, session_id="s1", attempt=1)
    missing_db = tmp_path / "no-such-dir" / "opencode.db"
    monkeypatch.setattr(session_store, "db_path", lambda: missing_db)

    decision = session_store.decide(
        proj, todo_id=TODO, stage=STAGE, role=ROLE, agent=ROLE, model="",
        attempt=2, config=_config("on"),
        logs_dir=proj / ".agentic" / "logs",
    )

    assert decision.reused is False
    assert decision.reason == "session not found", decision.reason
    pair = session_store.read_session_file(proj, TODO, STAGE)
    assert pair is not None and pair["session_id"] == "s1"
    assert "status" not in pair, "сбой чтения базы не делает сессию мёртвой"


# TODO-0309 (1): the capture must be proven on a COPY of the live opencode
# schema — the 5-column fake hid the real table's NOT NULL columns and
# defaults. A debug of the live base starts with this shape, not the fake.
_LIVE_SESSION_SCHEMA = """
CREATE TABLE `session` (
  `id` text PRIMARY KEY,
  `project_id` text NOT NULL,
  `workspace_id` text,
  `parent_id` text,
  `slug` text NOT NULL,
  `directory` text NOT NULL,
  `path` text,
  `title` text NOT NULL,
  `version` text NOT NULL,
  `share_url` text,
  `summary_additions` integer,
  `summary_deletions` integer,
  `summary_files` integer,
  `summary_diffs` text,
  `metadata` text,
  `cost` real DEFAULT 0 NOT NULL,
  `tokens_input` integer DEFAULT 0 NOT NULL,
  `tokens_output` integer DEFAULT 0 NOT NULL,
  `tokens_reasoning` integer DEFAULT 0 NOT NULL,
  `tokens_cache_read` integer DEFAULT 0 NOT NULL,
  `tokens_cache_write` integer DEFAULT 0 NOT NULL,
  `revert` text,
  `permission` text,
  `agent` text,
  `model` text,
  `time_created` integer NOT NULL,
  `time_updated` integer NOT NULL,
  `time_compacting` integer,
  `time_archived` integer
)
"""


def _make_live_schema_db(path):
    con = sqlite3.connect(path)
    con.execute(_LIVE_SESSION_SCHEMA)
    con.commit()
    con.close()
    return path


def _insert_live(db, sid, title, tc, *, directory=HOME_DIR):
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO session (id, project_id, slug, directory, title, version, "
        "agent, model, time_created, time_updated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "prj_1", "slug-1", directory, title, "1.19.4", "worker",
         '{"id":"m","providerID":"p"}', tc, tc),
    )
    con.commit()
    con.close()


def test_capture_on_a_copy_of_the_live_schema(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    db = _make_live_schema_db(tmp_path / "opencode.db")
    now = int(time.time() * 1000)
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    _insert_live(db, "live-1", title, now)
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    logs = proj / ".agentic" / "logs"

    sid = session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title=title, agent=ROLE, model="",
        attempt=1, spawn_started_ms=now, logs_dir=logs,
    )

    assert sid == "live-1"
    pair = session_store.read_session_file(proj, TODO, STAGE)
    assert set(pair) == {"session_id", "title", "agent", "model", "attempt", "ts"}
    assert pair["session_id"] == "live-1"
    assert "session capture: live-1" in _log_text(proj)


def test_capture_miss_names_the_search_in_the_reason(tmp_path, monkeypatch):
    # The live diagnosis line: a wrong title or window must be readable
    # without re-deriving what the code searched for.
    proj = _project(tmp_path)
    db = _make_live_schema_db(tmp_path / "opencode.db")
    now = int(time.time() * 1000)
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    logs = proj / ".agentic" / "logs"

    assert session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title="awf-miss", agent=ROLE,
        model="", attempt=1, spawn_started_ms=now, logs_dir=logs,
    ) is None

    text = _log_text(proj)
    assert "session capture: not found" in text
    assert "title=awf-miss" in text
    assert f"dir={proj}" in text


def test_capture_miss_reason_names_the_window_the_query_used(
    tmp_path, monkeypatch
):
    # TODO-0309 (architect review AR-0309-2): the diagnostic must quote the
    # SAME window the lookup used — a debugger must not hunt a title that
    # was simply outside the bounds. A row 3s before the early bound is
    # deliberately missed: if the query and the line drift apart, this
    # row either gets found (capture succeeds) or the quoted start moves.
    proj = _project(tmp_path)
    db = _make_live_schema_db(tmp_path / "opencode.db")
    now = int(time.time() * 1000)
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    _insert_live(db, "too-early", title, now - 5_000)
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    logs = proj / ".agentic" / "logs"

    assert session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title=title, agent=ROLE, model="",
        attempt=1, spawn_started_ms=now, logs_dir=logs,
    ) is None

    text = _log_text(proj)
    start_raw, rest = text.split("window=", 1)[1].split("..", 1)
    end_raw = rest.split(",", 1)[0]
    start, end = int(start_raw), int(end_raw)
    assert start == now - session_store.CAPTURE_EARLY_MS
    assert (
        now + session_store.CAPTURE_LATE_MS
        <= end
        <= now + session_store.CAPTURE_LATE_MS + 2_000
    )


def test_capture_write_failure_names_the_reason(tmp_path, monkeypatch):
    # TODO-0309 (1): every capture outcome carries the "session capture:"
    # prefix. A failed pair write must be readable in the stage log (it is
    # the only trace when the file is missing) and must never raise.
    proj = _project(tmp_path)
    db = _make_live_schema_db(tmp_path / "opencode.db")
    now = int(time.time() * 1000)
    title = f"awf-{ROLE}-{STAGE}-{TODO}-a1"
    _insert_live(db, "s1", title, now)
    monkeypatch.setattr(session_store, "db_path", lambda: db)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(session_store, "_write_session_file", boom)
    logs = proj / ".agentic" / "logs"

    assert session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title=title, agent=ROLE, model="",
        attempt=1, spawn_started_ms=now, logs_dir=logs,
    ) is None
    assert "session capture: write failed: disk full" in _log_text(proj)
    assert not session_store.session_file(proj, TODO, STAGE).exists()


def test_confirm_reason_names_the_base_and_the_missing_id(
    tmp_path, monkeypatch
):
    # TODO-0309 (1): the continuation path logs the same "session capture:"
    # reason family — a wrong base names the path, a readable base without
    # the row names the id it looked for.
    proj = _project(tmp_path)
    logs = proj / ".agentic" / "logs"
    missing = tmp_path / "gone.db"
    monkeypatch.setattr(session_store, "db_path", lambda: missing)

    assert session_store.confirm_and_update(
        proj, todo_id=TODO, stage=STAGE, session_id="s9", title="t",
        agent=ROLE, model="", attempt=2, logs_dir=logs,
    ) is None
    assert f"(base: {missing})" in _log_text(proj)

    db = _make_live_schema_db(tmp_path / "opencode.db")
    monkeypatch.setattr(session_store, "db_path", lambda: db)
    assert session_store.confirm_and_update(
        proj, todo_id=TODO, stage=STAGE, session_id="missing-id", title="t",
        agent=ROLE, model="", attempt=2, logs_dir=logs,
    ) is None
    assert "session capture: not found (id=missing-id)" in _log_text(proj)


def test_capture_reason_names_the_base_path(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    missing = tmp_path / "no-such-dir" / "opencode.db"
    monkeypatch.setattr(session_store, "db_path", lambda: missing)
    logs = proj / ".agentic" / "logs"

    assert session_store.capture_after_spawn(
        proj, todo_id=TODO, stage=STAGE, title=f"awf-{ROLE}-{STAGE}-{TODO}-a1",
        agent=ROLE, model="", attempt=1,
        spawn_started_ms=int(time.time() * 1000), logs_dir=logs,
    ) is None

    text = _log_text(proj)
    assert "session capture: " in text
    assert f"(base: {missing})" in text


def test_fix_round_reuses_the_implementer_pair_not_the_qa_pair(
    tmp_path, monkeypatch
):
    # TODO-0309 (2): a fix round continues the implementer's session; the
    # QA stage keeps its own id. Both pairs are captured on the first entry.
    proj = _project(tmp_path)
    (proj / ".agentic" / "roles" / "agent-qa-review.md").write_text(
        "# qa role\n", encoding="utf-8"
    )
    db = _make_db(tmp_path / "opencode.db")
    seen = _runner(monkeypatch, db)
    impl_stage = _stage("agent-implementer", ROLE)
    qa_stage = Stage(name="agent-qa-review", role="agent-qa-review", kind="execute")

    _run(proj, monkeypatch, db, _config("fix-only"), stage=impl_stage)
    _run(proj, monkeypatch, db, _config("fix-only"), stage=qa_stage)

    impl_pair = session_store.read_session_file(proj, TODO, "agent-implementer")
    qa_pair = session_store.read_session_file(proj, TODO, "agent-qa-review")
    assert impl_pair is not None and qa_pair is not None
    assert impl_pair["session_id"] != qa_pair["session_id"]

    # The fix round: the implementer continues ITS session.
    _run(proj, monkeypatch, db, _config("fix-only"), stage=impl_stage)

    fixed = seen[-1]
    assert fixed[fixed.index("--session") + 1] == impl_pair["session_id"]
    assert f"session: reused {impl_pair['session_id']}" in _log_text(proj)
    assert session_store.read_session_file(proj, TODO, "agent-qa-review")[
        "session_id"
    ] == qa_pair["session_id"]
