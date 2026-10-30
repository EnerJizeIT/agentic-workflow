"""R5 (T14–T16, AC23–AC27): recent models and ``opencode models``.

The reader lives in ``awf.opencode_db``; the CLI and the plugin import it
instead of keeping their own SQL (D8). The v2 model field and the
``--standalone`` support of ``models`` are IMP1 constants, not guesses.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from awf import opencode_db as db


def _v1(tmp_path: Path) -> Path:
    path = tmp_path / "v1.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            model TEXT, cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,
            time_created INTEGER, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,
            session_id TEXT, time_created INTEGER, data TEXT);
        """
    )
    con.execute(
        "INSERT INTO session VALUES ('s1','t','/p','{\"providerID\":\"vllm\",\"id\":\"llm\"}',"
        "0,0,0,0,0,1,1)"
    )
    con.execute(
        "INSERT INTO session VALUES ('s2','t','/p','plain/model',0,0,0,0,0,2,2)"
    )
    con.commit()
    con.close()
    return path


def _v2(tmp_path: Path) -> Path:
    path = tmp_path / "v2.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE session_v2 (id TEXT PRIMARY KEY, title TEXT, directory TEXT,
            model TEXT, cost REAL DEFAULT 0, tokens_input INTEGER DEFAULT 0,
            tokens_output INTEGER DEFAULT 0, tokens_cache_read INTEGER DEFAULT 0,
            tokens_cache_write INTEGER DEFAULT 0,
            time_created INTEGER, time_updated INTEGER);
        CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT,
            type TEXT, seq INTEGER, time_created INTEGER, time_updated INTEGER,
            data TEXT);
        """
    )
    con.execute(
        "INSERT INTO session_v2 VALUES ('s1','t','/p',"
        "'{\"id\":\"llm\",\"providerID\":\"vllm\"}',0,0,0,0,0,1,1)"
    )
    con.commit()
    con.close()
    return path


def test_recent_models_v1(tmp_path):
    assert db.recent_models(_v1(tmp_path)) == ["plain/model", "vllm/llm"]


def test_recent_models_v2(tmp_path):
    assert db.recent_models(_v2(tmp_path)) == ["vllm/llm"]


def test_recent_models_v2_field_unconfirmed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "V2_MODEL_FIELD", None)
    assert db.recent_models(_v2(tmp_path)) == []
    assert "поле не подтверждено" in capsys.readouterr().err


def test_detect_schema_markers(tmp_path):
    con = sqlite3.connect(_v2(tmp_path))
    assert db.detect_schema(con) == db.SCHEMA_V2
    con.close()
    # A v1 database carries an EMPTY session_message table — not v2 (CTX3).
    path = tmp_path / "v1withmsg.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE session (id TEXT); CREATE TABLE part (id TEXT);"
        "CREATE TABLE session_message (id TEXT);"
    )
    assert db.detect_schema(con) == db.SCHEMA_V1
    con.close()


def test_shared_reader_for_cli_and_plugin(tmp_path, monkeypatch):
    """AC24: both consumers call ``awf.opencode_db.recent_models``."""
    calls: list[tuple] = []

    def fake(db_path, limit=8):
        calls.append((str(db_path), limit))
        return ["vllm/llm"]

    monkeypatch.setattr(db, "recent_models", fake)
    data_home = tmp_path / "data"
    (data_home / "opencode").mkdir(parents=True)
    (data_home / "opencode" / "opencode.db").write_bytes(b"")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))

    from agent_workflow_ui import opencode_config
    from awf.api import model_check

    assert model_check._load_recent_models(Path("/x/opencode.db")) == {"vllm/llm"}
    assert opencode_config.read_recent_models(limit=3) == ["vllm/llm"]
    assert len(calls) == 2


def _run_result(rc=0, out="", err=""):
    return type("R", (), {"returncode": rc, "stdout": out, "stderr": err})()


def test_available_models_parsing(monkeypatch):
    from awf import opencode_version as ov

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (1, 19, 4))
    )
    import subprocess

    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **kw: _run_result(0, "[notice]\nvllm/llm\nopencode/big\n"),
    )
    assert db.available_models() == ["vllm/llm", "opencode/big"]


@pytest.mark.parametrize(
    "result",
    [
        _run_result(0, ""),
        _run_result(1, "", "boom"),
    ],
)
def test_available_models_empty_and_failure_degrade(monkeypatch, result, capsys):
    import subprocess

    from awf import opencode_version as ov

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (1, 19, 4))
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result)
    assert db.available_models() == []
    assert capsys.readouterr().err


def test_available_models_timeout_and_oserror_degrade(monkeypatch, capsys):
    import subprocess

    from awf import opencode_version as ov

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="opencode", timeout=10)

    monkeypatch.setattr(subprocess, "run", boom)
    assert db.available_models() == []
    assert "failed" in capsys.readouterr().err

    def missing(*a, **kw):
        raise FileNotFoundError("opencode")

    monkeypatch.setattr(subprocess, "run", missing)
    assert db.available_models() == []
    assert "unavailable" in capsys.readouterr().err


def test_models_flag_only_for_v2_with_support(monkeypatch):
    import subprocess

    from awf import opencode_version as ov

    seen: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: seen.append(argv) or _run_result(0, "a/b\n")
    )

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (1, 19, 4))
    )
    db.available_models()
    assert seen[-1] == ["opencode", "models"]

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    monkeypatch.setattr(db, "MODELS_STANDALONE_SUPPORTED", True)
    db.available_models()
    assert seen[-1] == ["opencode", "models", "--standalone"]

    monkeypatch.setattr(db, "MODELS_STANDALONE_SUPPORTED", False)
    db.available_models()
    assert seen[-1] == ["opencode", "models"]


def test_real_2_2_3_models_output_parses(monkeypatch):
    """AC27: the sample IMP1 captured from the real binary parses cleanly."""
    import subprocess

    from awf import opencode_version as ov

    real = (
        "opencode/beam-free\nopencode/big-pickle\n"
        "opencode/deepseek-v4.1-flash-free\nopencode/flint-40b-free\n"
        "opencode/glm-5.3-flash-free\n"
    )
    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _run_result(0, real))
    assert db.available_models() == real.split()


def test_doctor_reports_models_flag_support(monkeypatch, tmp_path):
    from awf import doctor
    from awf import opencode_version as ov

    monkeypatch.setattr(
        "awf.opencode_version.probe_version", lambda *a, **kw: ov.Probe("ok", (2, 2, 3))
    )
    monkeypatch.setattr(db, "available_models", lambda *a, **kw: [])
    proj = tmp_path / "proj"
    (proj / ".agentic").mkdir(parents=True)
    report = doctor.run_doctor(proj)
    assert any("models" in line and "поддерживается" in line for line in report.lines)


def test_imp1_constants_are_filled():
    """D9: the values were measured on a real binary, not left as guesses."""
    assert db.MODELS_STANDALONE_SUPPORTED is True
    assert db.V2_MODEL_FIELD == ("session_v2", "model")
