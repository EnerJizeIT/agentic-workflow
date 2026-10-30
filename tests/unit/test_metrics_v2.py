"""R4 (T10–T13, AC17–AC22, AC36, AC41): metrics over v1 and v2 schemas.

The v2 payload shapes are the ones IMP1 captured from a real 2.2.3 binary
(provenance in ``IMP1_REAL_*`` below): tokens live in ``session_message.data``
of an assistant row, the project is ``session_v2.directory`` (the cwd of
``opencode run``), the model is a JSON blob in ``session_v2.model``.
"""
from __future__ import annotations

import json
import sqlite3

from conftest import _git_init

from awf import metrics as M

# IMP1, 2026-10-30, opencode 2.2.3, isolated HOME/XDG
# ($ /tmp/opencode-v2/bin/opencode run --standalone -m fake/fake-model "hi"
#  against a local OpenAI-compatible stub), read back with:
#   SELECT type, data FROM session_message WHERE session_id='ses_e736...Z44b7';
# The assistant row and its token shape are copied VERBATIM.
IMP1_REAL_ASSISTANT = (
    '{"time":{"created":1793359930771,"streamed":1793359930807,'
    '"completed":1793359930810},"agent":"build",'
    '"model":{"id":"fake-model","providerID":"fake","variant":"default"},'
    '"content":[{"type":"text","text":"OK"}],"finish":"stop","rawFinish":"stop",'
    '"cost":0,"tokens":{"input":6,"output":7,"reasoning":0,'
    '"cache":{"read":5,"write":0}}}'
)
IMP1_REAL_USER = '{"time":{"created":1793359930732},"text":"hi","files":[]}'


def _project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic" / "context").mkdir(parents=True)
    _git_init(proj)
    return proj


def _schema_v2(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, title TEXT, directory TEXT, model TEXT,
            cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_reasoning INTEGER DEFAULT 0,
            tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, time_updated INTEGER, data TEXT);
        """
    )
    return con


def _add_v2_session(con, sid, title, directory, ts=1_700_000_000_000):
    con.execute(
        "INSERT INTO session_v2 (id,title,directory,model,time_created,time_updated)"
        " VALUES (?,?,?,?,?,?)",
        (sid, title, str(directory), '{"id":"m","providerID":"p"}', ts, ts),
    )


def _add_message(con, sid, seq, tokens, cost=0.0, ts=1_700_000_000_000):
    data = json.dumps({"tokens": tokens, "cost": cost})
    con.execute(
        "INSERT INTO session_message (id,session_id,type,seq,time_created,"
        "time_updated,data) VALUES (?,?,?,?,?,?,?)",
        (f"m{sid}{seq}", sid, "assistant", seq, ts, ts, data),
    )


def _collect(tmp_path, proj, db):
    return M.collect_metrics(
        proj, db_path=db, models_path=tmp_path / "absent.json",
        out=tmp_path / "r.md",
    )


def test_v2_tokens_summed_from_message_data(tmp_path):
    proj = _project(tmp_path)
    db = tmp_path / "v2.db"
    con = _schema_v2(db)
    _add_v2_session(con, "s1", "awf-developer-TODO-0001", proj)
    _add_message(
        con, "s1", 1,
        {"input": 3000, "output": 300, "reasoning": 0,
         "cache": {"read": 1500, "write": 50}},
        cost=0.03,
    )
    _add_message(
        con, "s1", 2,
        {"input": 1000, "output": 100, "cache": {"read": 500, "write": 0}},
        cost=0.01,
    )
    con.commit()
    con.close()

    res = _collect(tmp_path, proj, db)

    assert res.totals["win"] == 4000       # cache not added to input
    assert res.totals["wout"] == 400
    assert res.totals["wcr"] == 2000
    assert res.totals["wcw"] == 50
    assert abs(res.totals["wcost"] - 0.04) < 1e-9
    assert res.totals["comp"] == 0          # v2 has no part/compactions
    assert res.exit_code == 0
    assert res.sessions_na == ""
    assert "Воркеры всего: input 4,000" in res.report


def test_real_v2_payload_from_imp1(tmp_path):
    proj = _project(tmp_path)
    db = tmp_path / "real.db"
    con = _schema_v2(db)
    _add_v2_session(con, "ses_real", "awf-developer-TODO-0001", proj)
    con.execute(
        "INSERT INTO session_message (id,session_id,type,seq,time_created,"
        "time_updated,data) VALUES (?,?,?,?,?,?,?)",
        ("msg_real_user", "ses_real", "user", 4, 1, 1, IMP1_REAL_USER),
    )
    con.execute(
        "INSERT INTO session_message (id,session_id,type,seq,time_created,"
        "time_updated,data) VALUES (?,?,?,?,?,?,?)",
        ("msg_real_asst", "ses_real", "assistant", 5, 1, 1, IMP1_REAL_ASSISTANT),
    )
    con.commit()
    con.close()

    res = _collect(tmp_path, proj, db)
    # The real 2.2.3 payload parses: 6 in / 7 out / 5 cache-read (cost 0 —
    # the stub model has no price, so opencode wrote 0).
    assert res.totals["win"] == 6
    assert res.totals["wout"] == 7
    assert res.totals["wcr"] == 5
    assert res.totals["wcost"] == 0.0


def test_v2_foreign_project_is_na_not_zero(tmp_path):
    proj = _project(tmp_path)
    db = tmp_path / "v2.db"
    con = _schema_v2(db)
    _add_v2_session(con, "s1", "awf-developer-TODO-0001", "/elsewhere")
    _add_message(con, "s1", 1, {"input": 10, "output": 1, "cache": {"read": 0, "write": 0}})
    con.commit()
    con.close()

    res = _collect(tmp_path, proj, db)
    assert res.workers_by_role == {}
    assert res.sessions_na == "сессии проекта в базе opencode не найдены"
    assert "n/a" in res.report
    assert res.exit_code == 0


def test_empty_v2_and_no_tables_are_na(tmp_path):
    proj = _project(tmp_path)
    db = tmp_path / "empty.db"
    con = _schema_v2(db)
    con.commit()
    con.close()
    res = _collect(tmp_path, proj, db)
    assert res.sessions_na == "в базе opencode нет сессий"
    assert "n/a" in res.report and res.exit_code == 0

    blank = tmp_path / "blank.db"
    sqlite3.connect(blank).close()
    res2 = _collect(tmp_path, proj, blank)
    assert res2.sessions_na == "в базе opencode нет таблиц сессий"
    assert res2.exit_code == 0


def _schema_v1(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
            session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    return con


def test_mixed_db_counts_each_session_once(tmp_path):
    proj = _project(tmp_path)
    db = tmp_path / "mixed.db"
    con = _schema_v2(db)
    # v1 tables added next to v2 — the "both" schema.
    con.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
            session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    ts = 1_700_000_000_000
    # Same id in both schemas: v2 must win (AC41).
    con.execute(
        "INSERT INTO session VALUES ('dup','awf-developer-TODO-0001',?,0,1,1,0,0,?,?)",
        (str(proj), ts, ts),
    )
    con.execute(
        f"INSERT INTO part VALUES ('p1','m1','dup',?,'{proj}')",
        (ts,),
    )
    _add_v2_session(con, "dup", "awf-developer-TODO-0001", proj, ts)
    _add_message(con, "dup", 1, {"input": 4000, "output": 400, "cache": {"read": 2000, "write": 0}}, ts=ts)
    # v1-only session, another unit.
    con.execute(
        "INSERT INTO session VALUES ('s2','awf-qa-TODO-0002',?,0,100,10,0,0,?,?)",
        (str(proj), ts + 10, ts + 10),
    )
    con.execute(
        f"INSERT INTO part VALUES ('p2','m2','s2',?,'{proj}')",
        (ts + 10,),
    )
    con.commit()
    con.close()

    res = _collect(tmp_path, proj, db)
    units = {u["todo"]: u for u in res.units}

    assert units["TODO-0001"]["sessions"] == 1
    assert units["TODO-0001"]["w_in"] == 4000          # v2 row, not v1's 1
    assert units["TODO-0002"]["w_in"] == 100           # v1-only row still read
    assert res.totals["win"] == 4100                   # no double count


def test_v1_schema_unchanged(tmp_path):
    """AC17/AC20: the current v1 database keeps the previous numbers."""
    proj = _project(tmp_path)
    db = tmp_path / "v1.db"
    con = _schema_v1(db)
    con.execute(
        "INSERT INTO session VALUES ('s1','awf-developer-TODO-0001',?,0,1000,100,300,10,1,1)",
        (str(proj),),
    )
    con.execute(
        "INSERT INTO part VALUES ('p1','m1','s1',1,?)", (str(proj),)
    )
    con.commit()
    con.close()

    res = _collect(tmp_path, proj, db)
    assert res.totals["win"] == 1000
    assert res.totals["wout"] == 100
    assert res.totals["wcr"] == 300
    assert res.totals["wcw"] == 10
    assert res.sessions_na == ""
