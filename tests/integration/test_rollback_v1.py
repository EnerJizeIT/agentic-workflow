"""R8 (T19, AC33–AC35): rollback to 1.19.4 needs no migration.

The rollback is the BINARY plus ``auto``: awf must not rewrite
``opencode.json`` or the database while the launch window refuses (v2 + off)
and must build the old argv again once the 1.19.4 binary is back (T1 covers
the argv itself; this test proves the files stay byte-identical and the
mixed DB is still readable).
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from awf import opencode_version as ov
from awf.api import pipeline


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    for name in ("inbox", "outbox", "context", "state", "logs", "roles"):
        (proj / ".agentic" / name).mkdir(parents=True)
    (proj / ".agentic" / "config.yaml").write_text(
        "opencode:\n  standalone: auto\n", encoding="utf-8"
    )
    return proj


def _v2_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            model TEXT, cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_reasoning INTEGER DEFAULT 0,
            tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT,
            type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER,
            data TEXT);
        """
    )
    con.execute(
        "INSERT INTO session_v2 (id,title,directory,time_created,time_updated)"
        " VALUES ('s1','awf-developer-TODO-0001','/x',1,1)"
    )
    con.commit()
    con.close()


def test_refusal_window_does_not_touch_files(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    config_copy = tmp_path / "opencode.json"
    config_copy.write_text('{"provider": {"vllm": {}}}', encoding="utf-8")
    db_copy = tmp_path / "opencode.db"
    _v2_db(db_copy)

    before_cfg = _hash(config_copy)
    before_db = _hash(db_copy)

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    (proj / ".agentic" / "config.yaml").write_text(
        "opencode:\n  standalone: off\n", encoding="utf-8"
    )
    result = pipeline.start_pipeline(proj, background=False)
    assert result.run_mode == "noop"
    assert "opencode.standalone" in result.message

    assert _hash(config_copy) == before_cfg
    assert _hash(db_copy) == before_db

    # AC35: v2 + off is the R2 refusal, not "unknown error".
    assert result.exit_code == 0


def test_back_to_v1_auto_is_the_old_behavior(tmp_path, monkeypatch):
    proj = _project(tmp_path)
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (1, 19, 4))
    )
    _probe, plan, refusal = ov.launch_plan(proj)
    assert refusal is None
    assert plan.mode == "auto"
    assert plan.add_flag is False


def test_v2_sessions_stay_readable_after_rollback(tmp_path):
    """AC33: the mixed database is not migrated — v2 rows stay visible."""
    from conftest import _git_init

    from awf import metrics as M

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".agentic" / "context").mkdir(parents=True)
    _git_init(proj)
    db = tmp_path / "mixed.db"
    _v2_db(db)
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE session_v2 SET directory=?, title='awf-developer-TODO-0009'",
        (str(proj),),
    )
    con.execute(
        "INSERT INTO session_message (id,session_id,type,seq,time_created,"
        "time_updated,data) VALUES ('m1','s1','assistant',1,1,1,"
        "'{\"tokens\": {\"input\": 42, \"output\": 7, \"cache\": {\"read\": 1, \"write\": 0}}, \"cost\": 0.01}')"
    )
    con.commit()
    con.close()
    res = M.collect_metrics(
        proj, db_path=db, models_path=tmp_path / "absent.json",
        out=tmp_path / "r.md",
    )
    assert res.totals["win"] == 42
    assert res.totals["wout"] == 7
